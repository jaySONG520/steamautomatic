import datetime
import os
import random
import sys
import time

import schedule
import requests

# 添加项目根目录到 Python 路径（用于独立运行）
if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json5
import uuyoupinapi
from utils.logger import PluginLogger, handle_caught_exception, logger
from utils.notifier import send_notification
from utils.tools import exit_code
from utils.uu_helper import get_valid_token_for_uu

# 将sale_price_cache从实例变量改为模块级变量
sale_price_cache = {}


class UUAutoSellItem:
    def __init__(self, steam_client, steam_client_mutex, config):
        self.logger = PluginLogger("UUAutoSellItem")
        self.config = config
        self.timeSleep = 10.0
        self.inventory_list = []
        self.buy_price_cache = {}
        self.sale_inventory_list = None
        self.steam_client = steam_client
        # CSQAQ API 配置（用于获取租金和年化率）
        self._csqaq_api_token = None
        self._csqaq_base_url = "https://api.csqaq.com/api/v1"

    def init(self) -> bool:
        return False

    def get_uu_sale_inventory(self):
        try:
            sale_inventory_list = self.uuyoupin.get_sell_list()
            self.logger.info(f"已上架物品数量 {len(sale_inventory_list)}")
            self.sale_inventory_list = sale_inventory_list
            return sale_inventory_list
        except Exception as e:
            self.logger.error(f"获取UU上架物品失败! 错误: {e}", exc_info=True)
            return []

    def get_market_sale_price(self, item_id, cnt=10, good_name=None, buy_price=0):
        """
        获取市场出售价格
        :param item_id: 物品模板ID
        :param cnt: 获取前N个最低价
        :param good_name: 物品名称（用于日志）
        :param buy_price: 买入成本价（用于止损计算）
        :return: 建议出售价格
        """
        if item_id in sale_price_cache:
            if datetime.datetime.now() - sale_price_cache[item_id]["cache_time"] <= datetime.timedelta(minutes=5):
                commodity_name = sale_price_cache[item_id]["commodity_name"]
                cached_price = sale_price_cache[item_id]["sale_price"]
                # 注意：如果是止损模式，缓存可能需要重新计算，这里暂且保留原样
                # 但如果是止损场景，应该跳过缓存，因为市场价可能已经变化
                if self.config["uu_auto_sell_item"].get("enable_stop_loss", False) and buy_price > 0:
                    # 止损模式下，缩短缓存时间或跳过缓存，这里选择跳过缓存以确保实时性
                    self.logger.debug(f"{commodity_name} 止损模式启用，跳过缓存，重新计算价格")
                else:
                    self.logger.info(f"{commodity_name} 使用缓存结果，出售价格： {cached_price:.2f}")
                    return cached_price

        try:
            sale_price_rsp = self.uuyoupin.get_market_sale_list_with_abrade(item_id).json()
        except Exception as e:
            # 处理代理异常或其他网络错误
            error_msg = str(e)
            if "proxy" in error_msg.lower() or "ProxyError" in error_msg:
                self.logger.error(f"代理异常。建议关闭代理。如果你连接Steam有困难，可单独打开配置文件内的Steam代理功能。")
            else:
                self.logger.error(f"获取市场价格失败: {e}")
            raise  # 重新抛出异常，让调用者处理
        
        # 兼容大小写：Code 或 code
        code = sale_price_rsp.get("Code")
        if code is None:
            code = sale_price_rsp.get("code", -1)
        
        if code == 0:
            # 兼容大小写：Data 或 data
            rsp_list = sale_price_rsp.get("Data") or sale_price_rsp.get("data", [])
            rsp_cnt = len(rsp_list)
            if rsp_cnt == 0:
                sale_price = 0
                commodity_name = ""
                self.logger.warning(f"市场上没有指定筛选条件的物品")
                return sale_price
            commodity_name = rsp_list[0].get("commodityName") or rsp_list[0].get("CommodityName", "")

            sale_price_list = []
            cnt = min(cnt, rsp_cnt)
            for i in range(cnt):
                price = rsp_list[i].get("price") or rsp_list[i].get("Price")
                if price and i < cnt:
                    sale_price_list.append(float(price))

            # === 核心逻辑修改：异常值剔除 (Outlier Detection) ===
            if not sale_price_list:
                base_market_price = 0
            elif len(sale_price_list) == 1:
                base_market_price = sale_price_list[0]
            else:
                # 确保价格是从低到高排序的
                sale_price_list.sort()
                
                p1 = sale_price_list[0]  # 最低价
                p2 = sale_price_list[1]  # 次低价
                
                # 策略：如果第一名比第二名便宜太多（例如超过 10%），视为"杀猪盘"或"钓鱼单"
                # 阈值可以根据需要调整，这里设为 0.1 (10%)
                outlier_threshold = 0.1
                
                if (p2 - p1) / p2 > outlier_threshold:
                    self.logger.warning(f"⚠️ 检测到异常低价！最低价 {p1:.2f} 比次低价 {p2:.2f} 便宜超过 {outlier_threshold*100}%，判定为砸盘/钓鱼单。")
                    self.logger.warning(f"🛡️ 已剔除异常值 {p1:.2f}，将跟随次低价 {p2:.2f} 定价。")
                    # 剔除 p1，跟随 p2 定价
                    # 注意：具体的压价逻辑在外面统一处理，这里只确定"基准市场价"
                    base_market_price = p2 
                else:
                    # 正常情况，跟随最低价
                    base_market_price = p1

            # =======================================================
            # 🔥 新增逻辑：止损跑路检测 (Stop-Loss / Panic Sell)
            # =======================================================
            final_price = base_market_price
            
            # 必须开启止损功能 且 能够获取到买入成本 且 基准市场价有效
            if (self.config["uu_auto_sell_item"].get("enable_stop_loss", False) and 
                buy_price > 0 and 
                base_market_price > 0):
                
                stop_loss_ratio = self.config["uu_auto_sell_item"].get("stop_loss_ratio", 0.15)  # 默认亏15%止损
                panic_discount = self.config["uu_auto_sell_item"].get("panic_sell_discount", 0.01)  # 默认比市场价低1%
                
                # 计算当前亏损率： (成本 - 市场价) / 成本
                # 如果市场价 80，成本 100，亏损率 0.2 (20%)
                current_loss_ratio = (buy_price - base_market_price) / buy_price
                
                if current_loss_ratio >= stop_loss_ratio:
                    self.logger.warning(f"🚨 {commodity_name} 触发止损熔断！")
                    self.logger.warning(f"📉 成本: {buy_price:.2f}, 当前市场: {base_market_price:.2f}, 亏损率: {current_loss_ratio:.2%}")
                    
                    # 跑路策略：为了必定成交，在当前有效的最低价基础上，再降价一定比例
                    # 注意：这里我们用 p1 (真实的最低价) 而不是剔除后的 base_market_price
                    # 因为都要跑路了，我们要比那个砸盘的人更狠一点，或者紧贴着他卖
                    real_lowest_price = sale_price_list[0]  # 真实最低价（可能是砸盘价）
                    panic_price = real_lowest_price * (1 - panic_discount)
                    
                    # 还是要做个底线保护，防止价格计算出错变成 0 或负数
                    if panic_price > 0:
                        final_price = panic_price
                        self.logger.warning(f"🏃‍♂️ 执行跑路定价策略：{real_lowest_price:.2f} -> {final_price:.2f} (折扣 {panic_discount:.1%})")
                    else:
                        self.logger.error(f"⚠️ 跑路价格计算出错 ({panic_price:.2f})，使用基准市场价 {base_market_price:.2f}")
                        final_price = base_market_price
            
            self.logger.info(f"物品：{commodity_name} | 成本：{buy_price:.2f} | 市场最低：{sale_price_list[0] if sale_price_list else 0:.2f} | 基准市场价：{base_market_price:.2f} | 最终定价：{final_price:.2f}")
        else:
            final_price = 0
            commodity_name = ""
            msg = sale_price_rsp.get("Msg") or sale_price_rsp.get("msg", "未知错误")
            self.logger.error(f"查询出售价格失败，返回结果：{msg} (code: {code})，全部内容：{sale_price_rsp}")

        final_price = round(final_price, 2)

        if final_price != 0:
            sale_price_cache[item_id] = {
                "commodity_name": commodity_name,
                "sale_price": final_price,
                "cache_time": datetime.datetime.now(),
            }

        return final_price

    def _get_csqaq_api_token(self):
        """获取 CSQAQ API Token"""
        if self._csqaq_api_token:
            return self._csqaq_api_token
        
        invest_config = self.config.get("uu_auto_invest", {})
        self._csqaq_api_token = invest_config.get("csqaq_api_token", "")
        return self._csqaq_api_token

    def _get_good_id_from_csqaq(self, item_name):
        """
        通过物品名称搜索获取 CSQAQ 的 good_id
        增加重试机制，提高健壮性
        :param item_name: 物品名称（支持中文和英文，包含磨损信息）
        :return: good_id，如果未找到返回 None
        """
        api_token = self._get_csqaq_api_token()
        if not api_token:
            self.logger.debug(f"CSQAQ API Token 未配置，无法搜索 good_id")
            return None
        
        url = f"{self._csqaq_base_url}/info/get_good_id"
        headers = {
            "ApiToken": api_token,
            "Content-Type": "application/json"
        }
        payload = {
            "page_index": 1,
            "page_size": 20,
            "search": item_name
        }
        
        # 重试 3 次
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=15)  # 增加超时时间到15秒
                
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 200:
                        data = result.get("data", {}).get("data", {})
                        if data:
                            # 优先精确匹配：完全匹配中文名称或英文名称
                            exact_match = None
                            first_match = None
                            
                            for good_id_str, item_info in data.items():
                                if not isinstance(item_info, dict) or "id" not in item_info:
                                    continue
                                
                                # 保存第一个匹配项作为备选
                                if first_match is None:
                                    first_match = item_info["id"]
                                
                                # 检查是否完全匹配
                                csqaq_name = item_info.get("name", "")  # 中文名称
                                csqaq_market_hash_name = item_info.get("market_hash_name", "")  # 英文名称
                                
                                # 完全匹配中文名称或英文名称，或者包含完整磨损名称
                                if (item_name == csqaq_name or item_name == csqaq_market_hash_name or 
                                    item_name in csqaq_name):
                                    exact_match = item_info["id"]
                                    self.logger.debug(f"CSQAQ 精确匹配: {item_name} -> good_id={exact_match}")
                                    return exact_match
                            
                            # 如果没有精确匹配，返回第一个结果（API 通常按相关性排序）
                            if first_match:
                                self.logger.debug(f"CSQAQ 模糊匹配: {item_name} -> good_id={first_match} (使用第一个结果)")
                                return first_match
                        else:
                            # 搜索无结果，无需重试
                            self.logger.debug(f"CSQAQ 搜索 good_id 无结果: {item_name}")
                            return None
                
                # 如果状态码不是200，等待后重试
                if attempt < 2:  # 最后一次不等待
                    wait_time = 1 + attempt  # 递增等待时间：1秒、2秒
                    self.logger.debug(f"CSQAQ 搜索 good_id 失败 (HTTP {resp.status_code})，等待 {wait_time} 秒后重试 ({attempt+1}/3)")
                    time.sleep(wait_time)
                else:
                    self.logger.debug(f"CSQAQ 搜索 good_id 失败: HTTP {resp.status_code} (已重试3次)")
                    return None
                
            except Exception as e:
                if attempt < 2:  # 最后一次不等待
                    wait_time = 1 + attempt  # 递增等待时间：1秒、2秒
                    self.logger.debug(f"CSQAQ 搜索 good_id 第 {attempt+1} 次异常: {e}，等待 {wait_time} 秒后重试")
                    time.sleep(wait_time)
                else:
                    self.logger.debug(f"CSQAQ 搜索 good_id 异常（已重试3次）: {e}")
                    return None
        
        return None

    def get_lease_price_and_apy(self, template_id, current_market_price):
        """
        从 CSQAQ API 获取当前饰品的日租金和年化收益率 (APY)
        :param template_id: 物品模板ID
        :param current_market_price: 当前市场价（用于计算APY）
        :return: (daily_rent, apy) 元组，如果获取失败返回 (0, 0)
        """
        if current_market_price <= 0:
            return 0, 0
        
        api_token = self._get_csqaq_api_token()
        if not api_token:
            # 如果没有 CSQAQ Token，尝试使用 UU API
            return self._get_lease_price_from_uu(template_id, current_market_price)
        
        url = f"{self._csqaq_base_url}/info/good"
        headers = {"ApiToken": api_token}
        params = {"id": int(template_id)}
        
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                # 如果 CSQAQ 失败，回退到 UU API
                return self._get_lease_price_from_uu(template_id, current_market_price)
            
            result = resp.json()
            if result.get("code") != 200:
                return self._get_lease_price_from_uu(template_id, current_market_price)
            
            goods_info = result.get("data", {}).get("goods_info", {})
            if not goods_info:
                return self._get_lease_price_from_uu(template_id, current_market_price)
            
            # 从 CSQAQ 获取日租金和年化率
            daily_rent = float(goods_info.get("yyyp_lease_price", 0) or 0)
            apy_percent = float(goods_info.get("yyyp_lease_annual", 0) or 0)  # CSQAQ 返回的是百分比，如 25.5 表示 25.5%
            apy = apy_percent / 100.0  # 转换为小数，如 0.255 表示 25.5%
            
            # 如果 CSQAQ 没有年化率，但有日租金，手动计算
            if daily_rent > 0 and apy == 0:
                apy = (daily_rent * 365) / current_market_price
            
            return daily_rent, apy
            
        except Exception as e:
            self.logger.debug(f"CSQAQ 获取租金失败: {e}，回退到 UU API")
            return self._get_lease_price_from_uu(template_id, current_market_price)

    def _get_lease_price_from_uu(self, template_id, current_market_price):
        """
        从 UU API 获取租金（备用方案）
        """
        if not hasattr(self, 'uuyoupin') or self.uuyoupin is None:
            return 0, 0
        
        try:
            lease_list = self.uuyoupin.get_market_lease_price(template_id, cnt=5)
            if not lease_list:
                return 0, 0
            
            # 取前几个有效日租金的平均值
            unit_prices = []
            for item in lease_list:
                if hasattr(item, 'LeaseUnitPrice') and item.LeaseUnitPrice:
                    unit_prices.append(float(item.LeaseUnitPrice))
            
            if not unit_prices:
                return 0, 0
            
            avg_daily_rent = sum(unit_prices) / len(unit_prices)
            
            # 计算年化率 APY = (日租金 * 365) / 当前市场价
            apy = (avg_daily_rent * 365) / current_market_price if current_market_price > 0 else 0
            
            return avg_daily_rent, apy
            
        except Exception as e:
            self.logger.debug(f"UU API 获取租金失败: {e}")
            return 0, 0

    def get_days_remaining(self, item):
        """
        解析库存数据，计算剩余冷却天数
        支持多种格式：
        1. CacheExpirationDesc: "5天22小时" (优先)
        2. CacheExpiration: "2026-01-21 16:00:00" (备用)
        3. TradeCooldown: "2026-01-21 16:00:00" (备用)
        :param item: 库存物品数据
        :return: 剩余冷却天数（0表示已解冻或没有冷却期）
        """
        try:
            # 方法1: 优先从 CacheExpirationDesc 解析（格式："5天22小时"）
            cache_expiration_desc = item.get("CacheExpirationDesc", "")
            if cache_expiration_desc:
                try:
                    # 解析 "X天Y小时" 格式
                    import re
                    # 匹配 "X天" 和 "Y小时"
                    day_match = re.search(r'(\d+)天', cache_expiration_desc)
                    hour_match = re.search(r'(\d+)小时', cache_expiration_desc)
                    
                    days = 0
                    hours = 0
                    
                    if day_match:
                        days = int(day_match.group(1))
                    if hour_match:
                        hours = int(hour_match.group(1))
                    
                    # 如果有小时，向上取整（例如：5天22小时 = 6天）
                    if hours > 0:
                        days += 1
                    
                    if days > 0:
                        return days
                except Exception as e:
                    self.logger.debug(f"解析 CacheExpirationDesc 失败: {e}")
            
            # 方法2: 从 CacheExpiration 解析（格式："2026-01-21 16:00:00"）
            cache_expiration = item.get("CacheExpiration", "")
            if cache_expiration:
                try:
                    time_formats = [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%dT%H:%M:%S.%f",
                        "%Y/%m/%d %H:%M:%S",
                    ]
                    
                    cooldown_time = None
                    for fmt in time_formats:
                        try:
                            cooldown_time = datetime.datetime.strptime(str(cache_expiration), fmt)
                            break
                        except ValueError:
                            continue
                    
                    if cooldown_time:
                        now = datetime.datetime.now()
                        if cooldown_time > now:
                            delta = cooldown_time - now
                            days = delta.days
                            if delta.seconds > 0:
                                days += 1
                            return days
                except Exception as e:
                    self.logger.debug(f"解析 CacheExpiration 失败: {e}")
            
            # 方法3: 从 AssetInfo 或 item 中查找 TradeCooldown（备用）
            asset_info = item.get("AssetInfo", {})
            cooldown_str = (
                asset_info.get("TradeCooldown") or 
                asset_info.get("TradeCoolDown") or 
                asset_info.get("Cooldown") or
                asset_info.get("cooldown") or
                item.get("TradeCooldown") or
                item.get("TradeCoolDown") or
                item.get("Cooldown")
            )
            
            if cooldown_str:
                try:
                    time_formats = [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%dT%H:%M:%S.%f",
                        "%Y/%m/%d %H:%M:%S",
                    ]
                    
                    cooldown_time = None
                    for fmt in time_formats:
                        try:
                            cooldown_time = datetime.datetime.strptime(str(cooldown_str), fmt)
                            break
                        except ValueError:
                            continue
                    
                    if cooldown_time:
                        now = datetime.datetime.now()
                        if cooldown_time > now:
                            delta = cooldown_time - now
                            days = delta.days
                            if delta.seconds > 0:
                                days += 1
                            return days
                except Exception as e:
                    self.logger.debug(f"解析 TradeCooldown 失败: {e}")
            
            return 0  # 没有找到冷却时间，视为现货
            
        except Exception as e:
            self.logger.debug(f"解析冷却时间出错: {e}，默认按0天处理")
            return 0

    def sell_item(self, items):
        item_infos = items
        num = len(item_infos)
        if num == 0:
            self.logger.info(f"没有物品可以出售")
            return 0

        try:
            self.logger.info(f"正在调用上架接口，物品数量: {num}")
            self.logger.debug(f"上架数据: {item_infos}")
            
            rsp = self.uuyoupin.call_api(
                "POST",
                "/api/commodity/Inventory/SellInventoryWithLeaseV2",
                data={"GameId": "730", "itemInfos": item_infos},  # Csgo
            ).json()
            
            self.logger.debug(f"上架接口响应: {rsp}")
            
            # 兼容大小写：Code 或 code
            code = rsp.get("Code")
            if code is None:
                code = rsp.get("code", -1)
            
            if code == 0:
                # 尝试从响应中获取实际上架成功的数量
                success_count = len(item_infos)
                data_section = rsp.get("Data", {})
                if isinstance(data_section, dict) and "Commoditys" in data_section:
                    # 统计成功上架的数量
                    success_items = [c for c in data_section.get("Commoditys", []) if c.get("IsSuccess") == 1]
                    success_count = len(success_items)
                    if success_count < len(item_infos):
                        fail_items = [c for c in data_section.get("Commoditys", []) if c.get("IsSuccess") != 1]
                        for fail_item in fail_items:
                            comm_id = fail_item.get("CommodityId", "未知ID")
                            error_msg = fail_item.get("Message", "未知错误")
                            self.logger.warning(f"  ⚠️ 物品 {comm_id} 上架失败: {error_msg}")
                
                self.logger.info(f"✅ 成功上架 {success_count}/{num} 个物品")
                return success_count
            else:
                msg = rsp.get("Msg") or rsp.get("msg", "未知错误")
                self.logger.error(f"❌ 上架失败，返回结果：{msg} (code: {code})")
                self.logger.debug(f"完整响应: {rsp}")
                return -1
        except Exception as e:
            self.logger.error(f"❌ 调用 SellInventoryWithLeaseV2 上架失败: {e}", exc_info=True)
            return -1

    def change_sale_price(self, items):
        item_infos = items
        num = len(item_infos)
        if num == 0:
            self.logger.info(f"没有物品可以修改价格")
            return 0

        try:
            rsp = self.uuyoupin.call_api(
                "PUT",
                "/api/commodity/Commodity/PriceChangeWithLeaseV2",
                data={
                    "Commoditys": item_infos,
                },
            ).json()
            # 兼容大小写：Code 或 code
            code = rsp.get("Code")
            if code is None:
                code = rsp.get("code", -1)
            
            if code == 0:
                success_count = 0
                fail_count = 0
                data_section = rsp.get("Data", {})

                if isinstance(data_section, dict) and "Commoditys" in data_section:
                    total_processed = len(data_section["Commoditys"])
                    for commodity_result in data_section["Commoditys"]:
                        if commodity_result.get("IsSuccess") == 1:
                            success_count += 1
                        else:
                            fail_count += 1
                            error_msg = commodity_result.get("Message", "未知错误")
                            comm_id = commodity_result.get("CommodityId", "未知ID")
                            self.logger.error(f"修改商品 {comm_id} 价格失败: {error_msg}")

                    if "SuccessCount" in data_section:
                        success_count = data_section.get("SuccessCount", success_count)
                        fail_count = data_section.get("FailCount", fail_count)

                if total_processed == 0 and success_count == 0 and fail_count == 0:
                    success_count = num

                self.logger.info(f"尝试修改 {num} 个物品价格，成功 {success_count} 个，失败 {fail_count} 个")
                return success_count
            else:
                msg = rsp.get("Msg") or rsp.get("msg", "未知错误")
                code = rsp.get("Code") or rsp.get("code", -1)
                self.logger.error(f"修改出售价格失败，返回结果：{msg} (code: {code})，全部内容：{rsp}")
                return -1
        except Exception as e:
            self.logger.error(f"调用 PriceChangeWithLeaseV2 修改价格失败: {e}", exc_info=True)
            return -1

    def auto_sell(self):
        self.logger.info("悠悠有品出售自动上架插件已启动")
        self.logger.info("=" * 60)
        self.logger.info("开始扫描库存并分析租售决策")
        self.logger.info("=" * 60)
        self.operate_sleep()

        if self.uuyoupin is not None:
            try:
                sale_item_list = []
                self.uuyoupin.send_device_info()
                self.logger.info("正在获取悠悠有品库存...")

                self.inventory_list = self.uuyoupin.get_inventory(refresh=True)
                self.logger.info(f"库存总数: {len(self.inventory_list)} 件")

                # 获取已上架物品列表（用于检查是否重复上架）
                try:
                    sale_inventory_list = self.get_uu_sale_inventory()
                    # 构建已上架物品的 asset_id 集合，便于快速查找
                    on_sale_asset_ids = set()
                    for sale_item in sale_inventory_list:
                        sale_asset_id = sale_item.get("SteamAssetId") or sale_item.get("AssetId")
                        if sale_asset_id:
                            on_sale_asset_ids.add(str(sale_asset_id))
                    self.logger.info(f"已上架物品数量: {len(on_sale_asset_ids)} 件")
                except Exception as e:
                    self.logger.warning(f"获取已上架物品列表失败: {e}，将跳过重复检查")
                    on_sale_asset_ids = set()

                # 统计信息
                total_analyzed = 0
                total_sell = 0
                total_lease = 0
                total_hold = 0
                total_skipped = 0

                for i, item in enumerate(self.inventory_list):
                    if item.get("AssetInfo") is None:
                        continue
                    
                    asset_id = item.get("SteamAssetId")
                    item_id = item.get("TemplateInfo", {}).get("Id")
                    # 使用完整名称（包含磨损信息）进行CSQAQ搜索
                    full_name = item.get("TemplateInfo", {}).get("CommodityName") or item.get("ShotName", "未知")
                    market_price = item.get("TemplateInfo", {}).get("MarkPrice", 0)
                    
                    # 提取购入价
                    buy_price_str = item.get("AssetBuyPrice", "0").replace("购￥", "")
                    try:
                        buy_price = float(buy_price_str)
                    except:
                        buy_price = 0

                    self.buy_price_cache[item_id] = buy_price

                    # 跳过成本价为0的物品（无法进行盈亏分析）
                    if buy_price <= 0:
                        total_skipped += 1
                        continue

                    # 跳过市场价为0的物品（无法进行价格分析）
                    if market_price <= 0:
                        total_skipped += 1
                        continue

                    # 检查是否可交易
                    asset_status = item.get("AssetStatus", 0)
                    is_tradable = item.get("Tradable", False) is not False and asset_status == 0
                    
                    # =======================================================
                    # 【预售功能已注释】计算剩余冷却天数（用于判断是否可预售）
                    # =======================================================
                    # days_left = self.get_days_remaining(item)
                    days_left = 0  # 临时设置为0，禁用预售功能
                    
                    # 检查是否已在出售列表中
                    is_on_sale = str(asset_id) in on_sale_asset_ids
                    
                    # 日志输出
                    self.logger.info(f"\n[{i+1}/{len(self.inventory_list)}] 分析: {full_name}")
                    if is_on_sale:
                        tradable_status = f"已上架出售中(AssetStatus={asset_status})"
                    elif is_tradable:
                        tradable_status = "可交易（现货）"
                    # =======================================================
                    # 【预售功能已注释】预售状态判断
                    # =======================================================
                    # elif 0 < days_left <= 30:
                    #     tradable_status = f"可预售（冷却剩余 {days_left} 天，AssetStatus={asset_status}）"
                    # elif days_left > 30:
                    #     tradable_status = f"冷却期过长（{days_left}天 > 30天，AssetStatus={asset_status}）"
                    else:
                        tradable_status = f"不可交易(AssetStatus={asset_status})"
                    price_discount = (market_price - buy_price) / buy_price if buy_price > 0 else 0
                    self.logger.info(f"  状态: {tradable_status} | 市场价: {market_price:.2f}元 | 购入价: {buy_price:.2f}元 | 价差: {price_discount:.2%}")
                    
                    # 尝试获取 CSQAQ 数据
                    good_id = self._get_good_id_from_csqaq(full_name)
                    
                    yyyp_sell_price = 0
                    daily_rent = 0
                    apy = 0
                    
                    if good_id:
                        # 获取详细信息（带重试机制）
                        api_token = self._get_csqaq_api_token()
                        if api_token:
                            url = f"{self._csqaq_base_url}/info/good"
                            headers = {"ApiToken": api_token}
                            params = {"id": good_id}
                            
                            # 重试 3 次获取详情
                            goods_info = None
                            for attempt in range(3):
                                try:
                                    resp = requests.get(url, headers=headers, params=params, timeout=15)
                                    if resp.status_code == 200:
                                        result = resp.json()
                                        if result.get("code") == 200:
                                            goods_info = result.get("data", {}).get("goods_info", {})
                                            if goods_info:
                                                # 成功获取，跳出重试循环
                                                break
                                    
                                    # 如果状态码不是200，等待后重试
                                    if attempt < 2:
                                        wait_time = 1 + attempt
                                        self.logger.debug(f"  CSQAQ 详情请求失败 (HTTP {resp.status_code})，等待 {wait_time} 秒后重试 ({attempt+1}/3)")
                                        time.sleep(wait_time)
                                    
                                except Exception as e:
                                    if attempt < 2:
                                        wait_time = 1 + attempt
                                        self.logger.debug(f"  CSQAQ 详情请求异常，等待 {wait_time} 秒后重试 ({attempt+1}/3): {e}")
                                        time.sleep(wait_time)
                            
                            if goods_info:
                                # 提取关键信息
                                yyyp_sell_price = float(goods_info.get("yyyp_sell_price", 0) or 0)
                                daily_rent = float(goods_info.get("yyyp_lease_price", 0) or 0)
                                apy = float(goods_info.get("yyyp_lease_annual", 0) or 0) / 100.0  # 转换为小数
                                
                                self.logger.info(f"  ✅ CSQAQ 数据: 在售价={yyyp_sell_price:.2f}元 | 日租={daily_rent:.2f}元 | 年化率={apy:.2%}")
                            else:
                                self.logger.warning(f"  ⚠️ 无法从 CSQAQ 获取详细信息，启用兜底策略")
                        else:
                            self.logger.warning(f"  ⚠️ 未配置 CSQAQ Token，启用兜底策略")
                    else:
                        self.logger.warning(f"  ⚠️ 无法从 CSQAQ 获取 good_id，启用兜底策略")
                    
                    # 【兜底策略】如果 CSQAQ 彻底失效，使用悠悠有品的市场价和默认值
                    if yyyp_sell_price == 0:
                        yyyp_sell_price = market_price
                        self.logger.info(f"  📊 兜底策略: 使用悠悠市场价 {market_price:.2f}元")
                    
                    # 如果租金数据缺失，尝试从 UU API 获取（备用方案）
                    if daily_rent == 0 and apy == 0:
                        try:
                            daily_rent, apy = self._get_lease_price_from_uu(item_id, market_price)
                            if daily_rent > 0:
                                self.logger.info(f"  📊 兜底策略: 从 UU API 获取租金数据，日租={daily_rent:.2f}元 | 年化率={apy:.2%}")
                        except:
                            pass  # UU API 也失败，继续使用默认值（apy=0）
                    
                    # 进行租售决策（即使 CSQAQ 失败，只要有 buy_price 和 market_price，依然可以根据盈亏比例做决策）
                    try:
                        decision = self._make_rent_or_sell_decision(
                            full_name, buy_price, market_price, yyyp_sell_price, 
                            daily_rent, apy
                        )
                        
                        total_analyzed += 1
                        
                        # 根据决策结果统计
                        if decision == "出售":
                            total_sell += 1
                        elif decision == "出租":
                            total_lease += 1
                        else:
                            total_hold += 1
                        
                        self.logger.info(f"  💡 决策: {decision}")
                        
                        # =======================================================
                        # 【预售功能已注释】解锁预售逻辑（优化状态判断）
                        # =======================================================
                        
                        # 如果已在出售列表中，跳过（避免重复上架）
                        if is_on_sale:
                            self.logger.info(f"  ⚠️ 物品已在出售列表中，跳过上架")
                            continue
                        
                        # 允许上架的条件：
                        # 1. 现货 (is_tradable) - AssetStatus=0
                        # =======================================================
                        # 【预售功能已注释】预售相关条件判断
                        # =======================================================
                        # 2. 或者 处于预售期 (冷却天数 > 0 且 <= 30天) - AssetStatus 可以是 1 或 3
                        # 3. 或者 AssetStatus=1/3 但不在出售列表中（可能是状态异常，但可以尝试上架）
                        # 注意：AssetStatus=0 表示在库，AssetStatus=1/3 可能是冷却期或已上架
                        can_list = False
                        if is_tradable:
                            # 现货，可以直接上架
                            can_list = True
                        # =======================================================
                        # 【预售功能已注释】预售期上架判断
                        # =======================================================
                        # elif 0 < days_left <= 30:
                        #     # 预售期，允许上架（无论 AssetStatus 是多少）
                        #     can_list = True
                        # elif asset_status in [1, 3] and not is_on_sale:
                        #     # AssetStatus=1 或 3，但不在出售列表中，可能是冷却期但 days_left 计算失败
                        #     # 或者状态异常，尝试允许上架（让 API 来判断）
                        #     can_list = True
                        #     self.logger.debug(f"  ⚠️ AssetStatus={asset_status} 且不在出售列表中，尝试允许上架")
                        
                        # 只有决策为"出售"且可上架时，才执行出售操作
                        if decision == "出售" and can_list:
                            # 检查黑名单（支持精确匹配和模糊匹配）
                            blacklist_words = self.config["uu_auto_sell_item"].get("blacklist_words", [])
                            if blacklist_words:
                                is_blacklisted = False
                                for blacklist_item in blacklist_words:
                                    if not blacklist_item:
                                        continue
                                    # 判断是精确匹配还是模糊匹配
                                    # 如果黑名单项包含括号（磨损信息），则精确匹配；否则模糊匹配
                                    if "(" in blacklist_item and ")" in blacklist_item:
                                        # 精确匹配：完全匹配物品名称
                                        if blacklist_item == full_name:
                                            is_blacklisted = True
                                            self.logger.info(f"  ⚠️ 命中黑名单（精确匹配）：{blacklist_item}")
                                            break
                                    else:
                                        # 模糊匹配：匹配物品名称的一部分
                                        if blacklist_item in full_name:
                                            is_blacklisted = True
                                            self.logger.info(f"  ⚠️ 命中黑名单（模糊匹配）：{blacklist_item}")
                                            break
                                
                                if is_blacklisted:
                                    continue
                            
                            # 获取出售价格
                            try:
                                sale_price = self.get_market_sale_price(item_id, good_name=full_name, buy_price=buy_price)
                            except Exception as e:
                                handle_caught_exception(e, "UUAutoSellItem", known=True)
                                self.logger.error(f"  获取 {full_name} 的市场价格失败: {e}，暂时跳过")
                                continue
                            
                            if sale_price == 0:
                                self.logger.warning(f"  ⚠️ 出售价格为0，跳过")
                                continue
                            
                            # =======================================================
                            # 最低价格限制：小于100元不进行出售
                            # =======================================================
                            min_price = self.config["uu_auto_sell_item"].get("min_on_sale_price", 100)
                            if sale_price < min_price:
                                self.logger.info(f"  ⚠️ 价格低于最低限制({min_price}元)，跳过上架（当前价格: {sale_price:.2f}元）")
                                continue
                            
                            # =======================================================
                            # 【预售功能已注释】预售时间衰减定价策略 (Presale Pricing)
                            # =======================================================
                            # 
                            # # 获取配置的日折价率（建议在 config.json5 中添加 "cooldown_discount_rate": 0.01）
                            # # 如果没配置，默认 1% (0.01)
                            # discount_rate = self.config["uu_auto_sell_item"].get("cooldown_discount_rate", 0.01)
                            # 
                            # if days_left > 0:
                            #     # 计算折扣系数：1 - (天数 * 日折价率)
                            #     # 例如：剩 7 天，折价率 1% -> 系数 0.93 (93折)
                            #     discount_factor = 1 - (days_left * discount_rate)
                            #     
                            #     # 确保折扣系数不会为负数（最多打 0 折，即免费）
                            #     discount_factor = max(0, discount_factor)
                            #     
                            #     # 价格调整
                            #     original_price = sale_price
                            #     sale_price = sale_price * discount_factor
                            #     
                            #     self.logger.info(f"  ⏳ [预售模式] 冷却剩余 {days_left} 天，执行折价: {original_price:.2f}元 -> {sale_price:.2f}元 (折扣: {discount_factor:.2%})")
                            # else:
                            #     self.logger.debug(f"  ⚡ 现货商品，保持基准市场价")
                            # 
                            # =======================================================
                            
                            # 止盈策略
                            if self.config["uu_auto_sell_item"].get("take_profile", False):
                                self.logger.info(f"  按{self.config['uu_auto_sell_item']['take_profile_ratio']:.2f}止盈率设置价格")
                                if buy_price > 0:
                                    sale_price = max(sale_price, self.get_take_profile_price(buy_price))
                                    self.logger.info(f"  最终出售价格{sale_price:.2f}")
                            
                            # 价格调整
                            price_threshold = self.config["uu_auto_sell_item"].get("price_adjustment_threshold", 1.0)
                            if self.config["uu_auto_sell_item"].get("use_price_adjustment", True):
                                if sale_price > price_threshold:
                                    sale_price = max(price_threshold, sale_price - 0.01)
                                    sale_price = round(sale_price, 2)
                            
                            # 最高价格限制
                            max_price = self.config["uu_auto_sell_item"].get("max_on_sale_price", 0)
                            if max_price > 0 and sale_price > max_price:
                                self.logger.info(f"  ⚠️ 价格超过最高限制({max_price}元)，跳过上架")
                                continue
                            
                            self.logger.warning(f"  ✅ 即将上架：{full_name} 价格：{sale_price:.2f}元")
                            
                            sale_item = {
                                "AssetId": asset_id,
                                "IsCanLease": False,
                                "IsCanSold": True,
                                "Price": sale_price,
                                "Remark": "",
                            }
                            
                            sale_item_list.append(sale_item)
                        elif decision == "出租" or decision == "保留":
                            self.logger.info(f"  🛑 策略决定暂不出售（决策: {decision}），继续持有/出租")
                        elif decision == "出售" and not can_list:
                            # 决策为出售，但不符合上架条件
                            if is_on_sale:
                                self.logger.info(f"  ⚠️ 决策为出售，但物品已在出售列表中，跳过")
                            # =======================================================
                            # 【预售功能已注释】预售相关错误提示
                            # =======================================================
                            # elif days_left > 30:
                            #     self.logger.info(f"  ⚠️ 决策为出售，但冷却期过长（{days_left}天 > 30天），无法上架预售")
                            elif asset_status not in [0, 1, 3]:
                                self.logger.info(f"  ⚠️ 决策为出售，但物品状态异常（AssetStatus={asset_status}），无法上架")
                            else:
                                self.logger.info(f"  ⚠️ 决策为出售，但物品不可交易，无法上架")
                        
                        # 避免请求过快
                        time.sleep(0.3)
                        
                    except Exception as e:
                        self.logger.error(f"  ❌ 处理失败: {e}")
                        total_skipped += 1
                        continue
                
                # 输出汇总
                self.logger.info("\n" + "=" * 60)
                self.logger.info("分析结果汇总")
                self.logger.info("=" * 60)
                self.logger.info(f"总计分析: {total_analyzed} 件物品")
                self.logger.info(f"建议出售: {total_sell} 件")
                self.logger.info(f"建议出租: {total_lease} 件")
                self.logger.info(f"建议保留: {total_hold} 件")
                self.logger.info(f"跳过物品: {total_skipped} 件（成本价为0或市场价为0或API失败）")
                
                # 执行出售
                if sale_item_list:
                    self.logger.info(f"\n准备上架 {len(sale_item_list)} 件物品...")
                    # 显示即将上架的物品详情
                    for idx, sale_item in enumerate(sale_item_list, 1):
                        self.logger.info(f"  [{idx}] AssetId: {sale_item.get('AssetId')}, Price: {sale_item.get('Price')}元")
                    self.operate_sleep()
                    result = self.sell_item(sale_item_list)
                    if result > 0:
                        self.logger.info(f"✅ 上架完成，成功上架 {result} 件物品")
                    elif result == 0:
                        self.logger.warning(f"⚠️ 上架完成，但没有物品被上架（可能已上架或状态异常）")
                    else:
                        self.logger.error(f"❌ 上架失败，请检查日志")
                else:
                    self.logger.info("\n没有需要上架的物品")

            except TypeError as e:
                handle_caught_exception(e, "UUAutoSellItem")
                self.logger.error("悠悠有品出售自动上架出现错误")
                exit_code.set(1)
                return 1
            except Exception as e:
                self.logger.error(e, exc_info=True)
                self.logger.info("出现未知错误, 稍后再试! ")
                try:
                    self.uuyoupin.get_user_nickname()
                except KeyError as e:
                    handle_caught_exception(e, "UUAutoSellItem", known=True)
                    self.logger.error("检测到悠悠有品登录已经失效,请重新登录")
                    send_notification(self.steam_client, "检测到悠悠有品登录已经失效,请重新登录", title="悠悠有品登录失效")
                    self.logger.error("由于登录失败，插件将自动退出")
                    exit_code.set(1)
                    return 1

    def auto_change_price(self):
        self.logger.info("悠悠有品出售自动修改价格已启动")
        self.operate_sleep()

        try:
            self.uuyoupin.send_device_info()
            self.logger.info("正在获取悠悠有品出售已上架物品...")
            self.get_uu_sale_inventory()

            new_sale_item_list = []
            if not self.sale_inventory_list:
                self.logger.info("没有可用于改价的在售物品")
                return
            for i, item in enumerate(self.sale_inventory_list):
                asset_id = item["id"]
                item_id = item["templateId"]
                short_name = item["name"]
                buy_price = self.buy_price_cache.get(item_id, 0)

                if not any((s and s in short_name) for s in self.config["uu_auto_sell_item"]["name"]):
                    continue

                # 检查黑名单（支持精确匹配和模糊匹配）
                blacklist_words = self.config["uu_auto_sell_item"].get("blacklist_words", [])
                if blacklist_words:
                    is_blacklisted = False
                    for blacklist_item in blacklist_words:
                        if not blacklist_item:
                            continue
                        # 判断是精确匹配还是模糊匹配
                        # 如果黑名单项包含括号（磨损信息），则精确匹配；否则模糊匹配
                        if "(" in blacklist_item and ")" in blacklist_item:
                            # 精确匹配：完全匹配物品名称
                            if blacklist_item == short_name:
                                is_blacklisted = True
                                self.logger.info(f"改价跳过：{short_name} 命中黑名单（精确匹配）：{blacklist_item}")
                                break
                        else:
                            # 模糊匹配：匹配物品名称的一部分
                            if blacklist_item in short_name:
                                is_blacklisted = True
                                self.logger.info(f"改价跳过：{short_name} 命中黑名单（模糊匹配）：{blacklist_item}")
                                break
                    
                    if is_blacklisted:
                        continue

                sale_price = self.get_market_sale_price(item_id, good_name=short_name, buy_price=buy_price)

                if self.config["uu_auto_sell_item"]["take_profile"]:
                    self.logger.info(f"按{self.config['uu_auto_sell_item']['take_profile_ratio']:.2f}止盈率设置价格")
                    if buy_price > 0:
                        self.logger.debug(sale_price)
                        self.logger.debug(self.get_take_profile_price(buy_price))
                        sale_price = max(sale_price, self.get_take_profile_price(buy_price))
                        self.logger.info(f"最终出售价格{sale_price:.2f}")

                if sale_price == 0:
                    continue

                price_threshold = self.config["uu_auto_sell_item"].get("price_adjustment_threshold", 1.0)
                if self.config["uu_auto_sell_item"].get("use_price_adjustment", True):
                    if sale_price > price_threshold:
                        sale_price = max(price_threshold, sale_price - 0.01)
                        sale_price = round(sale_price, 2)

                sale_item = {"CommodityId": asset_id, "IsCanLease": False, "IsCanSold": True, "Price": sale_price, "Remark": ""}
                new_sale_item_list.append(sale_item)

            self.logger.info(f"{len(new_sale_item_list)} 件物品可以更新出售价格")
            self.operate_sleep()
            self.change_sale_price(new_sale_item_list)

        except TypeError as e:
            handle_caught_exception(e, "UUAutoSellItem-AutoChangePrice")
            self.logger.error("悠悠有品出售自动上架出现错误")
            exit_code.set(1)
            return 1
        except Exception as e:
            self.logger.error(e, exc_info=True)
            self.logger.info("出现未知错误, 稍后再试! ")
            try:
                self.uuyoupin.get_user_nickname()
            except KeyError as e:
                handle_caught_exception(e, "UUAutoSellItem-AutoChangePrice", known=True)
                send_notification(self.steam_client, "检测到悠悠有品登录已经失效,请重新登录", title="悠悠有品登录失效")
                self.logger.error("检测到悠悠有品登录已经失效,请重新登录")
                self.logger.error("由于登录失败，插件将自动退出")
                exit_code.set(1)
                return 1

    def exec(self):
        self.uuyoupin = uuyoupinapi.UUAccount(get_valid_token_for_uu(self.steam_client))  # type: ignore
        if not self.uuyoupin:
            self.logger.error("由于登录失败，插件将自动退出")
            exit_code.set(1)
            return 1
        self.logger.info(f"以下物品会出售：{self.config['uu_auto_sell_item']['name']}")
        
        # 启动时立即执行一次（可通过配置控制）
        run_on_start = self.config["uu_auto_sell_item"].get("run_on_start", True)
        if run_on_start:
            self.logger.info("启动时立即执行出售自动上架...")
            self.auto_sell()
        else:
            self.logger.info("已禁用启动时自动执行，等待定时任务...")

        # 获取配置参数
        run_time = self.config["uu_auto_sell_item"].get("run_time", "12:00")
        interval = self.config["uu_auto_sell_item"].get("interval", 50)  # auto_change_price 的间隔
        auto_sell_interval = self.config["uu_auto_sell_item"].get("auto_sell_interval", 45)  # auto_sell 的间隔（默认45分钟）
        enable_time_weighted = self.config["uu_auto_sell_item"].get("enable_time_weighted_frequency", False)  # 是否启用分时段策略
        
        # 修复时间格式：将点号替换为冒号（schedule库要求 HH:MM 格式）
        if "." in run_time:
            run_time = run_time.replace(".", ":")
            self.logger.warning(f"时间格式已自动修正：{run_time}（请使用 HH:MM 格式，例如 15:30）")

        # =======================================================
        # 🔥 核心修改：智能调度策略（止损+租售决策）
        # =======================================================
        if enable_time_weighted:
            # 高级策略：分时段执行（Time-Weighted Frequency）
            self.logger.info("=" * 60)
            self.logger.info("🚀 已启用分时段执行策略（Time-Weighted Frequency）")
            self.logger.info("=" * 60)
            self.logger.info("📊 执行频率策略：")
            self.logger.info("  02:00 - 08:00 (深夜)：每 120 分钟执行一次（休眠省 API）")
            self.logger.info("  08:00 - 18:00 (白天)：每 45 分钟执行一次（正常监控）")
            self.logger.info("  18:00 - 24:00 (晚高峰)：每 30 分钟执行一次（高频监控止损）")
            self.logger.info("=" * 60)
            
            # 使用实例变量记录上次执行时间，避免递归调用导致任务堆积
            self._last_auto_sell_time = datetime.datetime.now()
            self._last_auto_sell_interval = None
            
            def get_interval_by_time():
                """根据当前时间返回执行间隔（分钟）"""
                current_hour = datetime.datetime.now().hour
                if 2 <= current_hour < 8:
                    return 120  # 深夜：每 120 分钟
                elif 8 <= current_hour < 18:
                    return 45   # 白天：每 45 分钟
                else:
                    return 30   # 晚高峰：每 30 分钟
            
            def check_and_run_auto_sell():
                """每分钟检查一次，根据时段决定是否执行"""
                current_time = datetime.datetime.now()
                current_interval = get_interval_by_time()
                
                # 如果时段发生变化，重置计时
                if self._last_auto_sell_interval != current_interval:
                    self._last_auto_sell_time = current_time
                    self._last_auto_sell_interval = current_interval
                    self.logger.info(f"⏰ 时段切换，当前间隔调整为 {current_interval} 分钟")
                    return
                
                # 检查是否到了执行时间
                elapsed = (current_time - self._last_auto_sell_time).total_seconds() / 60
                if elapsed >= current_interval:
                    self.logger.info(f"⏰ 执行 auto_sell（距上次执行 {elapsed:.1f} 分钟，当前时段间隔 {current_interval} 分钟）")
                    self.auto_sell()
                    self._last_auto_sell_time = current_time
            
            # 每分钟检查一次
            schedule.every(1).minutes.do(check_and_run_auto_sell)
        else:
            # 标准策略：固定间隔执行
            self.logger.info("=" * 60)
            self.logger.info(f"📊 [智能资管] 策略执行频率：每 {auto_sell_interval} 分钟一次")
            self.logger.info("=" * 60)
            self.logger.info(f"💡 提示：如需启用分时段策略，请在配置中设置 enable_time_weighted_frequency: true")
            self.logger.info("=" * 60)
            
            # 将 auto_sell 改为每 N 分钟执行一次（而不是每天一次）
            schedule.every(auto_sell_interval).minutes.do(self.auto_sell)
        
        # auto_change_price 保持原有逻辑（每 interval 分钟执行一次）
        self.logger.info(f"[自动修改价格] 每隔 {interval} 分钟执行一次")
        schedule.every(interval).minutes.do(self.auto_change_price)

        while True:
            schedule.run_pending()
            time.sleep(1)

    def operate_sleep(self, sleep=None):
        if sleep is None:
            random.seed()
            sleep = random.randint(5, 15)
        self.logger.info(f"为了避免频繁访问接口，操作间隔 {sleep} 秒")
        time.sleep(sleep)

    def get_take_profile_price(self, buy_price):
        take_profile_ratio = self.config["uu_auto_sell_item"]["take_profile_ratio"]
        return buy_price * (1 + take_profile_ratio)

    def test_scan_inventory_and_decide(self):
        """
        测试模块：扫描库存，发现低于市场价的商品，查询CSQAQ信息，进行租售决策
        """
        if not hasattr(self, 'uuyoupin') or self.uuyoupin is None:
            self.logger.error("UU 客户端未初始化，无法扫描库存")
            return
        
        self.logger.info("=" * 60)
        self.logger.info("开始扫描库存并分析租售决策")
        self.logger.info("=" * 60)
        
        try:
            # 1. 获取库存
            self.logger.info("正在获取悠悠有品库存...")
            self.uuyoupin.send_device_info()
            inventory_list = self.uuyoupin.get_inventory(refresh=True)
            self.logger.info(f"库存总数: {len(inventory_list)} 件")
            
            if not inventory_list:
                self.logger.warning("库存为空，无法进行分析")
                return
            
            # 2. 分析每个物品
            results = []
            for i, item in enumerate(inventory_list):
                if item.get("AssetInfo") is None:
                    continue
                
                asset_id = item.get("SteamAssetId")
                template_id = item.get("TemplateInfo", {}).get("Id")
                # 优先使用 CommodityName（包含完整磨损信息），如果没有则使用 ShotName
                full_name = item.get("TemplateInfo", {}).get("CommodityName") or item.get("ShotName", "未知")
                market_price = item.get("TemplateInfo", {}).get("MarkPrice", 0)
                
                # 提取购入价
                buy_price_str = item.get("AssetBuyPrice", "0").replace("购￥", "")
                try:
                    buy_price = float(buy_price_str)
                except:
                    buy_price = 0
                
                # 跳过成本价为0的物品（没有购入价，无法进行盈亏分析）
                if buy_price <= 0:
                    continue
                
                # 只跳过市场价为0的物品（无法进行价格分析）
                if market_price <= 0:
                    continue
                
                # 检查是否可交易（仅用于日志显示，不跳过）
                is_tradable = item.get("Tradable", False) is not False and item.get("AssetStatus", 0) == 0
                tradable_status = "可交易" if is_tradable else f"不可交易(AssetStatus={item.get('AssetStatus', 0)})"
                
                # 判断是否低于市场价（这里可以自定义阈值，比如低于市场价5%）
                price_discount = 0
                if buy_price > 0:
                    price_discount = (market_price - buy_price) / buy_price
                
                # 3. 通过名称搜索获取 good_id（使用完整名称，包含磨损信息）
                self.logger.info(f"\n[{i+1}/{len(inventory_list)}] 分析: {full_name}")
                self.logger.info(f"  状态: {tradable_status} | 市场价: {market_price:.2f}元 | 购入价: {buy_price:.2f}元 | 价差: {price_discount:.2%}")
                
                good_id = self._get_good_id_from_csqaq(full_name)
                if not good_id:
                    self.logger.warning(f"  ⚠️ 无法从 CSQAQ 获取 good_id，跳过")
                    continue
                
                # 4. 获取详细信息
                api_token = self._get_csqaq_api_token()
                if not api_token:
                    self.logger.warning(f"  ⚠️ 未配置 CSQAQ Token，跳过")
                    continue
                
                url = f"{self._csqaq_base_url}/info/good"
                headers = {"ApiToken": api_token}
                params = {"id": good_id}
                
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=10)
                    if resp.status_code != 200:
                        self.logger.warning(f"  ⚠️ CSQAQ API 请求失败: {resp.status_code}")
                        continue
                    
                    result = resp.json()
                    if result.get("code") != 200:
                        self.logger.warning(f"  ⚠️ CSQAQ 业务错误: {result.get('msg')}")
                        continue
                    
                    goods_info = result.get("data", {}).get("goods_info", {})
                    if not goods_info:
                        self.logger.warning(f"  ⚠️ 未获取到详细信息")
                        continue
                    
                    # 提取关键信息
                    yyyp_sell_price = float(goods_info.get("yyyp_sell_price", 0) or 0)
                    yyyp_lease_price = float(goods_info.get("yyyp_lease_price", 0) or 0)
                    yyyp_lease_annual = float(goods_info.get("yyyp_lease_annual", 0) or 0) / 100.0  # 转换为小数
                    
                    self.logger.info(f"  ✅ CSQAQ 数据: 在售价={yyyp_sell_price:.2f}元 | 日租={yyyp_lease_price:.2f}元 | 年化率={yyyp_lease_annual:.2%}")
                    
                    # 5. 进行租售决策
                    decision = self._make_rent_or_sell_decision(
                        full_name, buy_price, market_price, yyyp_sell_price, 
                        yyyp_lease_price, yyyp_lease_annual
                    )
                    
                    results.append({
                        "name": full_name,
                        "buy_price": buy_price,
                        "market_price": market_price,
                        "yyyp_sell_price": yyyp_sell_price,
                        "daily_rent": yyyp_lease_price,
                        "apy": yyyp_lease_annual,
                        "decision": decision
                    })
                    
                    # 避免请求过快
                    time.sleep(0.5)
                    
                except Exception as e:
                    self.logger.error(f"  ❌ 获取详细信息失败: {e}")
                    continue
            
            # 6. 输出汇总结果
            self.logger.info("\n" + "=" * 60)
            self.logger.info("分析结果汇总")
            self.logger.info("=" * 60)
            
            sell_count = sum(1 for r in results if r["decision"] == "出售")
            lease_count = sum(1 for r in results if r["decision"] == "出租")
            hold_count = sum(1 for r in results if r["decision"] == "保留")
            
            self.logger.info(f"总计分析: {len(results)} 件物品")
            self.logger.info(f"建议出售: {sell_count} 件")
            self.logger.info(f"建议出租: {lease_count} 件")
            self.logger.info(f"建议保留: {hold_count} 件")
            self.logger.info("\n详细决策:")
            
            for r in results:
                self.logger.info(f"\n{r['name']}")
                self.logger.info(f"  购入价: {r['buy_price']:.2f}元 | 市场价: {r['market_price']:.2f}元")
                self.logger.info(f"  日租金: {r['daily_rent']:.2f}元 | 年化率: {r['apy']:.2%}")
                self.logger.info(f"  💡 决策: {r['decision']}")
            
        except Exception as e:
            self.logger.error(f"扫描库存失败: {e}", exc_info=True)

    def _make_rent_or_sell_decision(self, item_name, buy_price, market_price, yyyp_sell_price, daily_rent, apy):
        """
        进行租售决策（复用租售平衡策略逻辑）
        增加兜底处理，确保在数据缺失时也能正常工作
        :return: "出售" | "出租" | "保留"
        """
        # 兜底处理：如果没有获取到任何价格数据，返回"保留"
        current_price = yyyp_sell_price if yyyp_sell_price > 0 else market_price
        if current_price <= 0:
            self.logger.debug(f"  ⚠️ {item_name} 价格数据缺失，决策: 保留")
            return "保留"
        
        # 如果没有购入价，无法判断盈亏
        if buy_price <= 0:
            # 如果年化率很高，建议出租；否则建议出售
            if apy > 0.30:
                return "出租"
            else:
                return "出售"
        
        # 计算浮动盈亏率
        pnl_ratio = (current_price - buy_price) / buy_price
        
        # 四象限决策逻辑
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


if __name__ == "__main__":
    """
    独立测试模式
    用法：
    python plugins/UUAutoSellItem.py
    """
    print("=" * 60)
    print("UUAutoSellItem 模块独立测试")
    print("=" * 60)
    print("提示：")
    print("1. 确保 config/config.json5 中已配置 uu_auto_sell_item")
    print("2. 确保 config/uu_token.txt 存在（悠悠有品登录 Token）")
    print("3. 确保已配置 CSQAQ API Token（用于获取租金和年化率）")
    print("=" * 60)
    print()
    
    try:
        # 加载配置
        config_path = "config/config.json5"
        if not os.path.exists(config_path):
            print(f"❌ 配置文件不存在: {config_path}")
            sys.exit(1)
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = json5.load(f)
        
        # 检查配置
        if not config.get("uu_auto_sell_item", {}).get("enable", False):
            print("⚠️  uu_auto_sell_item 未启用，但测试模式仍可运行")
        
        # 创建模拟的 steam_client
        class MockSteamClient:
            def __init__(self):
                self.username = "test_user"
        
        # 创建插件实例
        plugin = UUAutoSellItem(MockSteamClient(), None, config)
        
        # 初始化 UU 客户端
        print("正在初始化悠悠有品客户端...")
        token = get_valid_token_for_uu(plugin.steam_client)
        if not token:
            print("❌ 获取 Token 失败，请检查 config/uu_token.txt")
            sys.exit(1)
        
        plugin.uuyoupin = uuyoupinapi.UUAccount(token)
        print(f"✅ 悠悠有品登录成功: {plugin.uuyoupin.get_user_nickname()}")
        print()
        
        # 测试菜单
        print("请选择测试功能：")
        print("1. 扫描库存并分析租售决策（推荐）")
        print("2. 测试自动上架 (auto_sell)")
        print("3. 测试自动改价 (auto_change_price)")
        print("4. 测试获取市场价 (get_market_sale_price)")
        print("5. 测试获取租金和年化率 (get_lease_price_and_apy)")
        print("6. 测试通过名称搜索 good_id")
        print("0. 退出")
        print()
        
        choice = input("请输入选项 (0-6): ").strip()
        
        if choice == "1":
            print("\n>>> 开始扫描库存并分析租售决策 <<<")
            plugin.test_scan_inventory_and_decide()
        elif choice == "2":
            print("\n>>> 开始测试自动上架功能 <<<")
            plugin.auto_sell()
        elif choice == "3":
            print("\n>>> 开始测试自动改价功能 <<<")
            plugin.auto_change_price()
        elif choice == "4":
            print("\n>>> 测试获取市场价 <<<")
            item_id = input("请输入物品模板ID (templateId): ").strip()
            if item_id:
                try:
                    price = plugin.get_market_sale_price(int(item_id), buy_price=100)
                    print(f"✅ 获取成功，建议出售价格: {price:.2f} 元")
                except Exception as e:
                    print(f"❌ 获取失败: {e}")
        elif choice == "5":
            print("\n>>> 测试获取租金和年化率 <<<")
            item_id = input("请输入物品模板ID (templateId): ").strip()
            market_price = input("请输入当前市场价: ").strip()
            if item_id and market_price:
                try:
                    daily_rent, apy = plugin.get_lease_price_and_apy(int(item_id), float(market_price))
                    print(f"✅ 获取成功:")
                    print(f"   日租金: {daily_rent:.2f} 元")
                    print(f"   年化率: {apy:.2%}")
                except Exception as e:
                    print(f"❌ 获取失败: {e}")
        elif choice == "6":
            print("\n>>> 测试通过名称搜索 good_id <<<")
            item_name = input("请输入物品名称（支持中文/英文）: ").strip()
            if item_name:
                try:
                    good_id = plugin._get_good_id_from_csqaq(item_name)
                    if good_id:
                        print(f"✅ 找到 good_id: {good_id}")
                    else:
                        print("❌ 未找到匹配的物品")
                except Exception as e:
                    print(f"❌ 搜索失败: {e}")
        elif choice == "0":
            print("退出测试")
        else:
            print("无效选项")
    
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
