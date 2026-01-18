import datetime
import time

import json5
import numpy as np
import requests
import schedule

import uuyoupinapi
from utils.logger import PluginLogger, handle_caught_exception
from utils.models import LeaseAsset
from utils.notifier import send_notification
from utils.tools import exit_code, is_subsequence
from utils.uu_helper import get_valid_token_for_uu
from uuyoupinapi import models


class UUAutoLeaseItem:
    def __init__(self, steam_client, steam_client_mutex, config):
        self.logger = PluginLogger("UUAutoLeaseItem")
        self.config = config
        self.timeSleep = 10
        self.inventory_list = []
        self.lease_price_cache = {}
        self.compensation_type = 0
        self.steam_client = steam_client
        # CSQAQ API 配置
        invest_config = self.config.get("uu_auto_invest", {})
        self._csqaq_api_token = invest_config.get("csqaq_api_token", "")
        self._csqaq_base_url = "https://api.csqaq.com/api/v1"

    def _is_in_sell_blacklist(self, full_name: str) -> bool:
        blacklist_words = self.config.get("uu_auto_sell_item", {}).get("blacklist_words", [])
        if not blacklist_words or not full_name:
            return False
        for blacklist_item in blacklist_words:
            if not blacklist_item:
                continue
            # 精确匹配：包含磨损信息
            if "(" in blacklist_item and ")" in blacklist_item:
                if blacklist_item == full_name:
                    return True
            else:
                # 模糊匹配：名称包含即可
                if blacklist_item in full_name:
                    return True
        return False

    @property
    def leased_inventory_list(self) -> list:
        return self.uuyoupin.get_uu_leased_inventory()

    def init(self) -> bool:
        proxies = None
        if self.config["use_proxies"]:
            proxies = self.config["proxies"]
        if not get_valid_token_for_uu(self.steam_client, proxies=proxies):
            self.logger.error("悠悠有品登录失败！即将关闭程序！")
            exit_code.set(1)
            return True
        return False

    def get_lease_price(self, template_id, min_price=0, max_price=20000, cnt=15):
        if template_id in self.lease_price_cache:
            if datetime.datetime.now() - self.lease_price_cache[template_id]["cache_time"] <= datetime.timedelta(minutes=20):
                commodity_name = self.lease_price_cache[template_id]["commodity_name"]
                lease_unit_price = self.lease_price_cache[template_id]["lease_unit_price"]
                long_lease_unit_price = self.lease_price_cache[template_id]["long_lease_unit_price"]
                lease_deposit = self.lease_price_cache[template_id]["lease_deposit"]
                self.logger.info(f"物品 {commodity_name} 使用缓存价格设置，短租价格：{lease_unit_price:.2f}，长租价格：{long_lease_unit_price:.2f}，押金：{lease_deposit:.2f}")
                return {
                    "LeaseUnitPrice": lease_unit_price,
                    "LongLeaseUnitPrice": long_lease_unit_price,
                    "LeaseDeposit": lease_deposit,
                }
        max_price = 20000 if max_price == 0 else max_price
        rsp_list = self.uuyoupin.get_market_lease_price(template_id, min_price=min_price, max_price=max_price, cnt=cnt)
        if len(rsp_list) > 0:
            rsp_cnt = len(rsp_list)
            commodity_name = rsp_list[0].CommodityName

            lease_unit_price_list = []
            long_lease_unit_price_list = []
            lease_deposit_list = []
            for i, item in enumerate(rsp_list):
                if item.LeaseUnitPrice and i < min(10, rsp_cnt):
                    lease_unit_price_list.append(float(item.LeaseUnitPrice))
                    if item.LeaseDeposit:
                        lease_deposit_list.append(float(item.LeaseDeposit))
                if item.LongLeaseUnitPrice:
                    long_lease_unit_price_list.append(float(item.LongLeaseUnitPrice))

            lease_unit_price = float(np.mean(lease_unit_price_list)) * 0.97
            lease_unit_price = max(lease_unit_price, float(lease_unit_price_list[0]), 0.01)

            long_lease_unit_price = min(lease_unit_price * 0.98, float(np.mean(long_lease_unit_price_list)) * 0.95)
            if len(long_lease_unit_price_list) == 0:
                long_lease_unit_price = max(lease_unit_price - 0.01, 0.01)
            else:
                long_lease_unit_price = max(long_lease_unit_price, float(long_lease_unit_price_list[0]), 0.01)

            lease_deposit = max(float(np.mean(lease_deposit_list)) * 0.98, float(min(lease_deposit_list)))

            self.logger.info(f"短租参考价格：{lease_unit_price_list}，长租参考价格：{long_lease_unit_price_list}")
        else:
            lease_unit_price = long_lease_unit_price = lease_deposit = 0
            commodity_name = ""

        lease_unit_price = round(lease_unit_price, 2)
        long_lease_unit_price = min(round(long_lease_unit_price, 2), lease_unit_price)
        lease_deposit = round(lease_deposit, 2)

        if self.config["uu_auto_lease_item"]["enable_fix_lease_ratio"] and min_price > 0:
            ratio = self.config["uu_auto_lease_item"]["fix_lease_ratio"]
            lease_unit_price = max(lease_unit_price, min_price * ratio)
            long_lease_unit_price = max(long_lease_unit_price, lease_unit_price * 0.98)

            self.logger.info(f"物品 {commodity_name}，启用比例定价，市场价 {min_price}，租金比例 {ratio}")

        self.logger.info(f"物品 {commodity_name}，短租价格：{lease_unit_price:.2f}，长租价格：{long_lease_unit_price:.2f}，押金：{lease_deposit:.2f}")
        if lease_unit_price != 0:
            self.lease_price_cache[template_id] = {
                "commodity_name": commodity_name,
                "lease_unit_price": lease_unit_price,
                "long_lease_unit_price": long_lease_unit_price,
                "lease_deposit": lease_deposit,
                "cache_time": datetime.datetime.now(),
            }

        return {
            "LeaseUnitPrice": lease_unit_price,
            "LongLeaseUnitPrice": long_lease_unit_price,
            "LeaseDeposit": lease_deposit,
        }

    def auto_lease(self):
        self.logger.info("悠悠有品出租自动上架插件已启动")
        self.operate_sleep()
        if self.uuyoupin is not None:
            try:
                lease_item_list = []
                self.uuyoupin.send_device_info()
                self.logger.info("正在获取悠悠有品库存...")

                self.inventory_list = self.uuyoupin.get_inventory(refresh=True)

                for i, item in enumerate(self.inventory_list):
                    if item["AssetInfo"] is None:
                        continue
                    asset_id = item["SteamAssetId"]
                    template_id = item["TemplateInfo"]["Id"]
                    short_name = item["ShotName"]
                    full_name = item.get("TemplateInfo", {}).get("CommodityName") or short_name
                    price = item["TemplateInfo"]["MarkPrice"]
                    
                    # --- 新增逻辑：提取购入价并对比 ---
                    # 提取购入价（同出售插件逻辑）
                    buy_price_str = item.get("AssetBuyPrice", "0").replace("购￥", "")
                    try:
                        buy_price = float(buy_price_str)
                    except:
                        buy_price = 0
                    
                    # 如果开启了策略：使用租售决策逻辑（四象限策略）
                    if self.config["uu_auto_lease_item"].get("only_lease_below_cost", False):
                        if buy_price > 0:
                            # 使用租售决策逻辑替代简单的止盈线判断
                            decision = self._make_rent_or_sell_decision_for_lease(short_name, buy_price, price, template_id)
                            
                            if decision == "出售":
                                # 决策为出售，若命中出售黑名单则仍可出租
                                if self._is_in_sell_blacklist(full_name):
                                    self.logger.info(f"物品 {short_name} 命中出售黑名单，仍继续出租。")
                                else:
                                    # 决策为出售，跳过租赁，等待出售插件处理
                                    self.logger.info(f"物品 {short_name} 租售决策：出售，跳过租赁逻辑，等待出售插件处理。")
                                    continue
                            elif decision == "出租":
                                # 决策为出租，继续租赁流程
                                self.logger.debug(f"物品 {short_name} 租售决策：出租，继续租赁。")
                            # else: "保留" 或其他情况，继续租赁流程
                    # ----------------------------
                    
                    if (
                        price < self.config["uu_auto_lease_item"]["filter_price"]
                        or item["Tradable"] is False
                        or item["AssetStatus"] != 0
                        or any(s != "" and is_subsequence(s, short_name) for s in self.config["uu_auto_lease_item"]["filter_name"])
                    ):
                        continue
                    self.operate_sleep()

                    price_rsp = self.get_lease_price(template_id, min_price=price, max_price=price * 2)
                    if price_rsp["LeaseUnitPrice"] == 0:
                        continue

                    lease_item = models.UUOnLeaseShelfItem(
                        AssetId=asset_id,
                        IsCanLease=True,
                        IsCanSold=False,
                        LeaseMaxDays=self.config["uu_auto_lease_item"]["lease_max_days"],
                        LeaseUnitPrice=price_rsp["LeaseUnitPrice"],
                        LongLeaseUnitPrice=price_rsp["LongLeaseUnitPrice"],
                        LeaseDeposit=str(price_rsp["LeaseDeposit"]),
                        CompensationType=self.compensation_type,
                    )
                    if self.config["uu_auto_lease_item"]["lease_max_days"] <= 8:
                        lease_item.LongLeaseUnitPrice = None

                    lease_item_list.append(lease_item)

                self.logger.info(f"共 {len(lease_item_list)} 件物品可以出租。")

                self.operate_sleep()
                if len(lease_item_list) > 0:
                    success_count = self.uuyoupin.put_items_on_lease_shelf(lease_item_list)
                    if success_count > 0:
                        self.logger.info(f"成功上架 {success_count} 个物品。")
                    else:
                        self.logger.error("上架失败！请查看日志获得详细信息。")
                    if len(lease_item_list) - success_count > 0:
                        self.logger.error(f"有 {len(lease_item_list) - success_count} 个商品上架失败。")

            except TypeError as e:
                handle_caught_exception(e, "UUAutoLeaseItem")
                self.logger.error("悠悠有品出租出现错误。")
                exit_code.set(1)
                return 1
            except Exception as e:
                self.logger.error(e, exc_info=True)
                self.logger.info("出现未知错误, 稍后再试! ")
                try:
                    self.uuyoupin.get_user_nickname()
                except KeyError as e:
                    handle_caught_exception(e, "UUAutoLeaseItem", known=True)
                    send_notification(self.steam_client, "检测到悠悠有品登录已经失效,请重新登录", title="悠悠有品登录失效")
                    self.logger.error("检测到悠悠有品登录已经失效,请重新登录。")
                    self.logger.error("由于登录失败，插件将自动退出。")
                    exit_code.set(1)
                    return 1

    def auto_change_price(self):
        self.logger.info("悠悠出租自动修改价格已启动")
        self.operate_sleep(15)
        try:
            self.uuyoupin.send_device_info()
            self.logger.info("正在获取悠悠有品出租已上架物品...")
            leased_item_list = self.leased_inventory_list
            for i, item in enumerate(leased_item_list):
                template_id = item.templateid
                short_name = item.short_name
                price = item.price

                if any(s != "" and is_subsequence(s, short_name) for s in self.config["uu_auto_lease_item"]["filter_name"]):
                    continue

                price_rsp = self.get_lease_price(template_id, min_price=price, max_price=price * 2)
                if price_rsp["LeaseUnitPrice"] == 0:
                    continue

                item.LeaseUnitPrice = price_rsp["LeaseUnitPrice"]
                item.LongLeaseUnitPrice = price_rsp["LongLeaseUnitPrice"]
                item.LeaseDeposit = price_rsp["LeaseDeposit"]
                item.LeaseMaxDays = self.config["uu_auto_lease_item"]["lease_max_days"]
                if self.config["uu_auto_lease_item"]["lease_max_days"] <= 8:
                    item.LongLeaseUnitPrice = None

            self.logger.info(f"{len(leased_item_list)} 件物品可以更新出租价格。")
            self.operate_sleep()
            if len(leased_item_list) > 0:
                success_count = self.uuyoupin.change_leased_price(leased_item_list, compensation_type=self.compensation_type)
                self.logger.info(f"成功修改 {success_count} 件物品出租价格。")
                if len(leased_item_list) - success_count > 0:
                    self.logger.error(f"{len(leased_item_list) - success_count} 件物品出租价格修改失败。")
            else:
                self.logger.info(f"没有物品可以修改价格。")

        except TypeError as e:
            handle_caught_exception(e, "UUAutoLeaseItem-AutoChangePrice")
            self.logger.error("悠悠有品出租出现错误")
            exit_code.set(1)
            return 1
        except Exception as e:
            self.logger.error(e, exc_info=True)
            self.logger.info("出现未知错误, 稍后再试! ")
            try:
                self.uuyoupin.get_user_nickname()
            except KeyError as e:
                handle_caught_exception(e, "UUAutoLeaseItem-AutoChangePrice", known=True)
                self.logger.error("检测到悠悠有品登录已经失效,请重新登录")
                self.logger.error("由于登录失败，插件将自动退出")
                exit_code.set(1)
                return 1

    def auto_set_zero_cd(self):
        self.logger.info("悠悠有品出租自动设置0cd已启动")
        self.operate_sleep()
        if self.uuyoupin is not None:
            try:
                zero_cd_valid_list = self.uuyoupin.get_zero_cd_list()
                enable_zero_cd_list = []
                for order in zero_cd_valid_list:
                    name = order["commodityInfo"]["name"]
                    if any(s != "" and is_subsequence(s, name) for s in self.config["uu_auto_lease_item"]["filter_name"]):
                        continue
                    enable_zero_cd_list.append(int(order["orderId"]))
                self.logger.info(f"共 {len(enable_zero_cd_list)} 件物品可以设置为0cd。")
                if len(enable_zero_cd_list) > 0:
                    self.uuyoupin.enable_zero_cd(enable_zero_cd_list)
            except Exception as e:
                self.logger.error(e, exc_info=True)
                self.logger.info("出现未知错误, 稍后再试! ")

    def exec(self):
        self.logger.info(f"以下物品不会出租：{self.config['uu_auto_lease_item']['filter_name']}")
        if "compensation_type" in self.config["uu_auto_lease_item"]:
            self.compensation_type = self.config["uu_auto_lease_item"]["compensation_type"]

        self.uuyoupin = uuyoupinapi.UUAccount(get_valid_token_for_uu(self.steam_client))

        self.pre_check_price()
        self.auto_lease()
        self.auto_set_zero_cd()

        run_time = self.config["uu_auto_lease_item"]["run_time"]
        interval = self.config["uu_auto_lease_item"]["interval"]
        if "zero_cd_run_time" in self.config["uu_auto_lease_item"]:
            zero_cd_run_time = self.config["uu_auto_lease_item"]["zero_cd_run_time"]
        else:
            zero_cd_run_time = "23:30"
        self.logger.info(f"[自动出售] 等待到 {run_time} 开始执行。")
        self.logger.info(f"[自动修改价格] 每隔 {interval} 分钟执行一次。")
        self.logger.info(f"[设置0cd] 等待到 {zero_cd_run_time} 开始执行。")

        schedule.every().day.at(f"{run_time}").do(self.auto_lease)
        schedule.every(interval).minutes.do(self.auto_change_price)
        schedule.every().day.at(f"{zero_cd_run_time}").do(self.auto_set_zero_cd)

        while True:
            schedule.run_pending()
            time.sleep(1)

    def operate_sleep(self, sleep=None):
        if sleep is None:
            time.sleep(self.timeSleep)
        else:
            time.sleep(sleep)

    def _get_good_id_from_csqaq(self, item_name):
        """通过物品名称搜索获取 CSQAQ 的 good_id"""
        if not self._csqaq_api_token:
            return None
        
        url = f"{self._csqaq_base_url}/info/get_good_id"
        headers = {
            "ApiToken": self._csqaq_api_token,
            "Content-Type": "application/json"
        }
        payload = {
            "page_index": 1,
            "page_size": 20,
            "search": item_name
        }
        
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code != 200:
                return None
            
            result = resp.json()
            if result.get("code") != 200:
                return None
            
            data = result.get("data", {}).get("data", {})
            if not data:
                return None
            
            # 返回第一个匹配的 good_id
            for good_id_str, item_info in data.items():
                if isinstance(item_info, dict) and "id" in item_info:
                    return item_info["id"]
            
            return None
            
        except Exception as e:
            self.logger.debug(f"CSQAQ 搜索 good_id 失败: {e}")
            return None

    def _get_lease_price_and_apy_from_csqaq(self, template_id, current_market_price):
        """从 CSQAQ API 获取当前饰品的日租金和年化收益率 (APY)"""
        if current_market_price <= 0:
            return 0, 0
        
        if not self._csqaq_api_token:
            return 0, 0
        
        url = f"{self._csqaq_base_url}/info/good"
        headers = {"ApiToken": self._csqaq_api_token}
        params = {"id": int(template_id)}
        
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                return 0, 0
            
            result = resp.json()
            if result.get("code") != 200:
                return 0, 0
            
            goods_info = result.get("data", {}).get("goods_info", {})
            if not goods_info:
                return 0, 0
            
            # 从 CSQAQ 获取日租金和年化率
            daily_rent = float(goods_info.get("yyyp_lease_price", 0) or 0)
            apy_percent = float(goods_info.get("yyyp_lease_annual", 0) or 0)  # CSQAQ 返回的是百分比
            apy = apy_percent / 100.0  # 转换为小数
            
            # 如果 CSQAQ 没有年化率，但有日租金，手动计算
            if daily_rent > 0 and apy == 0:
                apy = (daily_rent * 365) / current_market_price
            
            return daily_rent, apy
            
        except Exception as e:
            self.logger.debug(f"CSQAQ 获取租金失败: {e}")
            return 0, 0

    def _make_rent_or_sell_decision_for_lease(self, item_name, buy_price, market_price, template_id):
        """
        进行租售决策（复用租售平衡策略逻辑）
        :return: "出售" | "出租" | "保留"
        """
        # 如果没有购入价，无法判断盈亏，默认出租
        if buy_price <= 0:
            return "出租"
        
        # 获取 CSQAQ 数据
        yyyp_sell_price = 0
        daily_rent = 0
        apy = 0
        
        # 尝试通过名称获取 good_id
        good_id = self._get_good_id_from_csqaq(item_name)
        if good_id:
            # 使用 good_id 获取详细信息
            url = f"{self._csqaq_base_url}/info/good"
            headers = {"ApiToken": self._csqaq_api_token}
            params = {"id": good_id}
            
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 200:
                        goods_info = result.get("data", {}).get("goods_info", {})
                        if goods_info:
                            yyyp_sell_price = float(goods_info.get("yyyp_sell_price", 0) or 0)
                            daily_rent = float(goods_info.get("yyyp_lease_price", 0) or 0)
                            apy_percent = float(goods_info.get("yyyp_lease_annual", 0) or 0)
                            apy = apy_percent / 100.0
                            
                            # 如果 CSQAQ 没有年化率，但有日租金，手动计算
                            if daily_rent > 0 and apy == 0:
                                current_price = yyyp_sell_price if yyyp_sell_price > 0 else market_price
                                if current_price > 0:
                                    apy = (daily_rent * 365) / current_price
            except:
                pass
        
        # 如果 CSQAQ 获取失败，尝试使用 template_id 直接获取
        if daily_rent == 0 and apy == 0:
            daily_rent, apy = self._get_lease_price_and_apy_from_csqaq(template_id, market_price)
        
        # 使用市场价作为当前价（如果没有CSQAQ在售价）
        current_price = yyyp_sell_price if yyyp_sell_price > 0 else market_price
        
        # 计算浮动盈亏率
        pnl_ratio = (current_price - buy_price) / buy_price
        
        # 四象限决策逻辑（与 UUAutoSellItem 保持一致）
        stop_loss_limit = -0.15
        
        # 场景 D: 深度亏损
        if pnl_ratio < stop_loss_limit:
            return "出售"  # 强制止损
        
        # 场景 B: 浮亏可控 + 高回报
        elif stop_loss_limit <= pnl_ratio < -0.05 and apy > 0.20:
            return "出租"  # 保留吃租
        
        # 场景 C: 浮亏可控 + 低回报
        elif stop_loss_limit <= pnl_ratio < -0.05 and apy <= 0.20:
            return "出售"  # 不值得持有
        
        # 场景 A: 盈利或微亏 (>-5%)
        else:
            # 如果盈利 < 10%，继续出租（吃租金）
            if pnl_ratio < 0.10:
                return "出租"  # 盈利不足10%，继续持有吃租
            # 如果盈利 >= 10%，且年化率很高，也继续出租
            elif apy > 0.60:
                return "出租"  # 现金奶牛，即使盈利也继续出租
            else:
                return "出售"  # 盈利>=10%且年化率不高，可以考虑出售

    def pre_check_price(self):
        self.get_lease_price(44444, 1000)
        self.logger.info("请检查押金获取是否有问题，如有请终止程序，否则开始运行该插件。")
        self.operate_sleep()


if __name__ == "__main__":
    # 调试代码
    with open("config/config.json5", "r", encoding="utf-8") as f:
        my_config = json5.load(f)

    uu_auto_lease = UUAutoLeaseItem(None, None, my_config)
    token = get_valid_token_for_uu(uu_auto_lease.steam_client)
    if not token:
        uu_auto_lease.logger.error("由于登录失败，插件将自动退出")
        exit_code.set(1)
    else:
        uu_auto_lease.uuyoupin = uuyoupinapi.UUAccount(token)
    uu_auto_lease.auto_change_price()
