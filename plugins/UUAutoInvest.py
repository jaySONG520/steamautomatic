import json
import os
import sys
import time
import random  # 用于随机延迟，模拟人类行为
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径（用于独立运行）
if __name__ == "__main__":
    # 获取当前文件所在目录的父目录（项目根目录）
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json5
import requests
import schedule

import uuyoupinapi
from utils.logger import PluginLogger, handle_caught_exception
from utils.tools import exit_code
from utils.uu_helper import get_valid_token_for_uu


# ==========================================
# 核心改造 1: 信号与执行器分离
# ==========================================

class SignalManager:
    """信号管理器：负责信号的落地存储（留痕）"""
    
    def __init__(self, logger):
        self.logger = logger
        self.signal_dir = os.path.join(os.getcwd(), "data", "signals")
        if not os.path.exists(self.signal_dir):
            os.makedirs(self.signal_dir)
    
    def save_signal(self, signal: dict):
        """将交易信号保存到文件，便于复盘"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            filename = os.path.join(self.signal_dir, f"{today}.json")
            
            # 追加写入模式（每行一个JSON，方便逐行读取）
            with open(filename, "a", encoding="utf-8") as f:
                f.write(json.dumps(signal, ensure_ascii=False) + "\n")
            
            self.logger.debug(f"信号已落地: {signal.get('name', '未知')}")
        except Exception as e:
            self.logger.error(f"保存信号失败: {e}")


class UUOrderExecutor:
    """执行器：只负责执行购买动作，不负责决策"""
    
    def __init__(self, uuyoupin_client, logger, config):
        self.uuyoupin = uuyoupin_client
        self.logger = logger
        self.config = config
    
    def execute_buy(self, signal: dict) -> bool:
        """
        执行具体的下单 API 调用
        :param signal: 经过校验的信号字典
        :return: 是否下单成功
        """
        template_id = signal["templateId"]
        market_hash_name = signal["marketHashName"]
        item_name = signal["name"]
        target_price = signal["target_price"]
        
        invest_config = self.config.get("uu_auto_invest", {})
        test_mode = invest_config.get("test_mode", False)
        
        try:
            # 测试模式
            if test_mode:
                self.logger.info(f"[测试模式] 模拟执行购买 -> {item_name} | 价格: {target_price:.2f}")
                return True
            
            # 真实下单
            self.logger.info(f"🚀 [执行器] 发起挂单 -> {item_name} | 价格: {target_price:.2f}")
            res = self.uuyoupin.publish_purchase_order(
                templateId=int(template_id),
                templateHashName=market_hash_name,
                commodityName=item_name,
                purchasePrice=target_price,
                purchaseNum=1
            )
            
            # 解析结果
            res_data = res.json()
            code = res_data.get("Code") or res_data.get("code", 0)
            
            if code == 0:
                order_no = res_data.get("Data", {}).get("orderNo", "未知")
                self.logger.info(f"✅ 挂单成功！订单号: {order_no}")
                return True
            elif code == 84101:
                # 登录状态失效，需要重新登录
                msg = res_data.get("Msg") or res_data.get("msg", "登录信息异常，请重新登录！")
                self.logger.error(f"⛔ 登录状态失效: {msg}")
                raise Exception("登录状态失效，请重新登录")  # 抛出异常，让上层处理
            else:
                msg = res_data.get("Msg") or res_data.get("msg", "未知错误")
                self.logger.warning(f"❌ 挂单失败: {msg} (code: {code})")
                return False
                
        except Exception as e:
            # 如果是登录失效异常，直接抛出，让上层处理
            if "登录状态失效" in str(e) or "84101" in str(e):
                raise
            self.logger.error(f"执行器异常: {e}")
            return False


class UUAutoInvest:
    """
    悠悠有品自动投资插件 (狙击防封版)
    策略：随机乱序、遇阻即停、慢速稳健
    """

    def __init__(self, steam_client, steam_client_mutex, config):
        self.logger = PluginLogger("UUAutoInvest")
        self.config = config
        self.steam_client = steam_client
        self.steam_client_mutex = steam_client_mutex
        self.uuyoupin = None
        
        # 内部组件（架构升级）
        self.signal_manager = None
        self.executor = None
        
        # API session（用于保持 cookie）
        self._api_session = None
        # 求购价缓存：{templateId: {"max_price": float, "sell_price": float, "good_id": int, "update_time": timestamp}}
        self._purchase_price_cache = {}
        self._cache_duration = 20 * 60  # 20分钟缓存
        # CSQAQ API 配置
        self._csqaq_api_token = None
        self._csqaq_base_url = "https://api.csqaq.com/api/v1"

    def init(self) -> bool:
        """初始化插件"""
        if not self.config.get("uu_auto_invest", {}).get("enable", False):
            return False

        token = get_valid_token_for_uu(self.steam_client)
        if not token:
            self.logger.error("登录失败，自动投资插件无法启动")
            return True

        try:
            self.uuyoupin = uuyoupinapi.UUAccount(token)
            
            # 初始化子组件（架构升级）
            self.signal_manager = SignalManager(self.logger)
            self.executor = UUOrderExecutor(self.uuyoupin, self.logger, self.config)
            
            self.logger.info("自动投资插件初始化成功 (架构升级版)")
            return False
        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error("自动投资插件初始化失败")
            return True

    def fetch_candidates_from_whitelist(self):
        """
        从 Scanner.py 生成的白名单读取候选饰品列表（仅选择 S/A 级优质资产）
        白名单文件：config/whitelist.json
        """
        candidates = []
        whitelist_file = "config/whitelist.json"
        
        if not os.path.exists(whitelist_file):
            self.logger.warning(f"未找到白名单文件: {whitelist_file}")
            self.logger.info("请先运行 Scanner.py 生成白名单")
            return []

        try:
            with open(whitelist_file, "r", encoding="utf-8") as f:
                whitelist_data = json5.load(f)

            # Scanner.py 生成的是数组格式
            if not isinstance(whitelist_data, list):
                self.logger.warning("白名单文件格式错误，应为数组格式")
                return []

            if not whitelist_data:
                self.logger.warning("白名单为空")
                return []

            self.logger.info(f"从白名单读取候选饰品（共 {len(whitelist_data)} 个）")

            valid_count = 0
            for item in whitelist_data:
                template_id = str(item.get("templateId", ""))
                name = item.get("name", "未知")
                tier = item.get("tier", "C")  # 资产分级
                roi = item.get("roi", 0)
                
                # === 核心改动：只选择 S/A 级优质资产 ===
                if tier not in ["S", "A"]:
                    continue
                
                # 价格以实时为准，我们不再依赖 buy_limit (求购价)，而是关注 current_price (售价)
                market_price = item.get("yyyp_sell_price", 0)
                if market_price <= 0:
                    continue

                candidates.append({
                    "templateId": template_id,
                    "name": name,
                    "market_price": market_price,  # 记录白名单时的售价作为参考
                    "tier": tier,                   # 记录评级
                    "roi": roi,
                })
                valid_count += 1

            self.logger.info(f"白名单共 {len(whitelist_data)} 个，精选出 {valid_count} 个核心资产 (S/A级)")
            return candidates

        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error(f"读取白名单文件失败: {e}")
            return []


    def get_item_details_from_uu(self, template_id):
        """
        获取饰品详情 + 实时最低售价
        返回: (detail_dict, is_system_busy)
        """
        try:
            # 获取在售列表 (pageSize=1 且默认排序通常是价格升序，取第一个就是最低价)
            res = self.uuyoupin.get_market_sale_list_with_abrade(
                int(template_id), pageIndex=1, pageSize=1
            )
            
            # 处理 HTTP 层面错误（429 Too Many Requests）
            if isinstance(res, requests.Response):
                if res.status_code == 429:
                    self.logger.warning("HTTP 429: 请求过于频繁")
                    return None, True  # True 表示系统繁忙
                market_data = res.json()
            else:
                market_data = res if isinstance(res, dict) else res.json()

            # 兼容大小写：Code 或 code
            code = market_data.get("Code")
            if code is None:
                code = market_data.get("code", -1)
            
            msg = market_data.get("Msg") or market_data.get("msg", "未知错误")
            
            # 判定系统繁忙的条件
            is_busy = (
                code == 84104 or
                code == 429 or
                "频繁" in msg or 
                "系统繁忙" in msg or
                code == -1
            )
            
            if is_busy:
                self.logger.warning(f"触发风控: {msg} (Code: {code})")
                return None, True
            
            if code != 0:
                self.logger.debug(f"获取详情失败: {msg} (Code: {code})")
                return None, False

            # Data 字段可能是列表或字典，需要兼容处理
            data = market_data.get("Data")
            if data is None:
                data = market_data.get("data")
            if not data:
                return None, False
            
            # 如果 Data 是列表，直接使用；如果是字典，尝试获取 CommodityList
            if isinstance(data, list):
                commodity_list = data
            else:
                commodity_list = data.get("CommodityList", [])
            
            if not commodity_list:
                return None, False

            detail = commodity_list[0]
            market_hash_name = detail.get("commodityHashName") or detail.get("MarketHashName", "")
            
            # === 核心修改：提取实时最低售价 ===
            # 注意：UU API 返回的 Price 通常是字符串，需要转 float
            lowest_sell_price = float(detail.get("Price", 0) or 0)

            if not market_hash_name or lowest_sell_price <= 0:
                self.logger.warning(f"无法获取 marketHashName 或最低售价无效")
                return None, False
            
            return {
                "marketHashName": market_hash_name,
                "lowest_sell_price": lowest_sell_price  # 返回最低售价
            }, False  # 成功时返回 False（不是系统繁忙）

        except Exception as e:
            self.logger.error(f"获取饰品 {template_id} 详情失败: {e}")
            return None, False

    def _get_csqaq_api_token(self):
        """获取 CSQAQ API Token"""
        if self._csqaq_api_token:
            return self._csqaq_api_token
        
        invest_config = self.config.get("uu_auto_invest", {})
        self._csqaq_api_token = invest_config.get("csqaq_api_token", "")
        return self._csqaq_api_token

    def _get_details_from_csqaq(self, template_id, item_name):
        """
        [新增] 替代 UU 接口：从 CSQAQ 获取实时售价和 HashName
        优点：完全不触发 UU 风控，安全查价
        """
        api_token = self._get_csqaq_api_token()
        if not api_token:
            self.logger.warning("未配置 CSQAQ Token，无法进行安全查价")
            return None

        url = f"{self._csqaq_base_url}/info/good"
        headers = {"ApiToken": api_token}
        params = {"id": int(template_id)}

        try:
            # 这里的请求发给 CSQAQ，不会触发 UU 的风控
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                self.logger.warning(f"CSQAQ API 请求失败: {resp.status_code}")
                return None

            result = resp.json()
            if result.get("code") != 200:
                self.logger.warning(f"CSQAQ 业务错误: {result.get('msg')}")
                return None

            goods_info = result.get("data", {}).get("goods_info", {})
            if not goods_info:
                return None

            # 提取关键信息
            market_hash_name = goods_info.get("market_hash_name", "")
            lowest_sell_price = float(goods_info.get("yyyp_sell_price", 0) or 0)

            if not market_hash_name:
                self.logger.warning(f"{item_name} 无法从 CSQAQ 获取 HashName")
                return None

            return {
                "marketHashName": market_hash_name,
                "lowest_sell_price": lowest_sell_price
            }

        except Exception as e:
            self.logger.error(f"CSQAQ 查价异常: {e}")
            return None


    def _get_optimal_purchase_price(self, template_id, item_name, recommended_price, market_price):
        """
        获取最优求购价：使用 CSQAQ API 的 chart 接口获取求购价和在售价
        20分钟缓存一次，求购价不能大于在售价
        :param template_id: 模板ID
        :param item_name: 商品名称（用于日志）
        :param recommended_price: 白名单推荐价格（备用）
        :param market_price: 市场价（用于验证）
        :return: 最优求购价（如果无法获取或求购价>在售价，返回0）
        """
        template_id_str = str(template_id)
        current_time = time.time()
        
        # 检查缓存是否有效
        cache_valid = False
        if template_id_str in self._purchase_price_cache:
            cache_data = self._purchase_price_cache[template_id_str]
            if current_time - cache_data.get("update_time", 0) < self._cache_duration:
                cache_valid = True
                max_purchase_price = cache_data.get("max_price", 0)
                sell_price = cache_data.get("sell_price", 0)
                self.logger.debug(f"{item_name} 使用缓存数据: 求购价={max_purchase_price:.2f}元, 在售价={sell_price:.2f}元")
        
        # 如果缓存无效，从 CSQAQ API 获取
        if not cache_valid:
            try:
                # templateId 就是 CSQAQ 的 good_id
                good_id = int(template_id)
                
                self.logger.info(f"{item_name} 正在从 CSQAQ API 获取求购价和在售价...")
                api_token = self._get_csqaq_api_token()
                if not api_token:
                    self.logger.warning(f"{item_name} 未配置 CSQAQ API Token，使用推荐价格")
                    if recommended_price > 0:
                        optimal_price = recommended_price
                    else:
                        optimal_price = round(market_price * 0.92, 2)
                    # 更新缓存
                    self._purchase_price_cache[template_id_str] = {
                        "max_price": optimal_price,
                        "sell_price": market_price,
                        "update_time": current_time
                    }
                    return optimal_price
                
                # 使用 /api/v1/info/good 接口获取实时求购价和在售价（比 chart 接口更准确）
                good_url = f"{self._csqaq_base_url}/info/good"
                headers = {
                    "ApiToken": api_token
                }
                params = {"id": good_id}
                
                time.sleep(0.5)  # 遵守频率限制
                resp = requests.get(good_url, headers=headers, params=params, timeout=10)
                
                # 解析响应
                max_purchase_price = 0
                sell_price = 0
                buy_num = 0
                sell_num = 0
                
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 200:
                        goods_info = result.get("data", {}).get("goods_info", {})
                        if goods_info:
                            # 直接从 goods_info 获取实时求购价和在售价
                            max_purchase_price = float(goods_info.get("yyyp_buy_price", 0) or 0)
                            sell_price = float(goods_info.get("yyyp_sell_price", 0) or 0)
                            buy_num = int(goods_info.get("yyyp_buy_num", 0) or 0)
                            sell_num = int(goods_info.get("yyyp_sell_num", 0) or 0)
                            
                            self.logger.debug(f"{item_name} CSQAQ API 返回: 求购价={max_purchase_price:.2f}元 (求购数={buy_num}), 在售价={sell_price:.2f}元 (在售数={sell_num})")
                        else:
                            self.logger.warning(f"{item_name} CSQAQ API 返回数据中无 goods_info")
                    else:
                        self.logger.warning(f"{item_name} CSQAQ API 返回错误: code={result.get('code')}, msg={result.get('msg')}")
                else:
                    self.logger.warning(f"{item_name} CSQAQ API 请求失败: HTTP {resp.status_code}")
                
                # 如果无法获取数据，使用推荐价格
                if max_purchase_price <= 0:
                    self.logger.warning(f"{item_name} 无法从 CSQAQ API 获取求购价，使用推荐价格")
                    if recommended_price > 0:
                        max_purchase_price = recommended_price
                    else:
                        max_purchase_price = round(market_price * 0.92, 2)
                
                if sell_price <= 0:
                    sell_price = market_price  # 使用传入的市场价作为备用
                
                # 更新缓存
                self._purchase_price_cache[template_id_str] = {
                    "max_price": max_purchase_price,
                    "sell_price": sell_price,
                    "update_time": current_time
                }
                
                self.logger.info(f"{item_name} 从 CSQAQ API 获取: 求购价={max_purchase_price:.2f}元 (求购数={buy_num}), 在售价={sell_price:.2f}元 (在售数={sell_num})（已缓存，20分钟内有效）")
                
            except Exception as e:
                self.logger.error(f"{item_name} 获取求购价异常: {e}，使用推荐价格")
                if recommended_price > 0:
                    max_purchase_price = recommended_price
                else:
                    max_purchase_price = round(market_price * 0.92, 2)
                sell_price = market_price
                # 即使异常也更新缓存，避免频繁请求
                self._purchase_price_cache[template_id_str] = {
                    "max_price": max_purchase_price,
                    "sell_price": sell_price,
                    "update_time": current_time
                }
        else:
            max_purchase_price = self._purchase_price_cache[template_id_str]["max_price"]
            sell_price = self._purchase_price_cache[template_id_str].get("sell_price", market_price)
        
        # 比最高求购价多1元
        optimal_price = round(max_purchase_price + 1.0, 2)
        
        # 关键逻辑：求购价不能大于在售价
        if optimal_price > sell_price:
            self.logger.warning(f"{item_name} 计算出的求购价 {optimal_price:.2f}元 > 在售价 {sell_price:.2f}元，调整为在售价-0.01元")
            optimal_price = round(sell_price - 0.01, 2)
            if optimal_price <= 0:
                self.logger.error(f"{item_name} 调整后的求购价无效 ({optimal_price:.2f}元)，返回0")
                return 0
        
        self.logger.info(f"{item_name} 最优求购价: {optimal_price:.2f}元 (最高求购价: {max_purchase_price:.2f}元 + 1元, 在售价: {sell_price:.2f}元)")
        
        return optimal_price

    # ==========================================
    # 核心改造 2: 二次确认校验 (Pre-Trade Check)
    # ==========================================
    
    def pre_trade_check(self, signal: dict, current_balance: float) -> bool:
        """
        下单前最后一次风控校验
        :param signal: 交易信号
        :param current_balance: 当前余额
        :return: 是否通过校验
        """
        item_name = signal["name"]
        target_price = signal["target_price"]
        market_price = signal["market_price"]
        
        # 1. 余额硬校验
        if current_balance < target_price:
            self.logger.warning(f"⛔ [风控拦截] {item_name} 余额不足 (缺 {target_price - current_balance:.2f}元)")
            return False
        
        # 2. 价格合理性校验 (允许 target_price == market_price，因为我们是按售价购买)
        # 如果求购价明显高于市场价（超过1%），可能是数据异常
        if target_price > market_price * 1.01:
            self.logger.warning(f"⛔ [风控拦截] {item_name} 价格异常 (求购 {target_price:.2f} > 市场 {market_price:.2f} * 1.01)")
            return False
        
        # 3. 价格区间最终校验
        invest_config = self.config.get("uu_auto_invest", {})
        min_price = invest_config.get("min_price", 100)
        max_price = invest_config.get("max_price", 2000)
        
        if not (min_price <= target_price <= max_price):
            self.logger.warning(f"⛔ [风控拦截] {item_name} 价格超出配置区间 ({target_price:.2f}元)")
            return False
        
        return True

    def execute_investment(self):
        """执行自动投资任务（重构版：信号驱动）"""
        self.logger.info(">>> 开始自动投资 (架构升级版) <<<")

        # 0. 确保子组件已初始化（防止直接调用 execute_investment 时未初始化）
        if self.signal_manager is None or self.executor is None:
            if self.uuyoupin is None:
                self.logger.error("UUAccount 未初始化，无法执行投资任务")
                return
            # 延迟初始化子组件
            self.signal_manager = SignalManager(self.logger)
            self.executor = UUOrderExecutor(self.uuyoupin, self.logger, self.config)
            self.logger.info("子组件已延迟初始化")

        # 1. 刷新余额并检查最低余额要求
        try:
            self.uuyoupin.refresh_balance()
            current_balance = self.uuyoupin.balance
            self.logger.info(f"当前可用余额: {current_balance:.2f}")

            invest_config = self.config.get("uu_auto_invest", {})
            min_balance_required = invest_config.get("min_balance_required", 100)  # 最低余额要求

            # 如果余额不足100元，不请求API也不购买
            if current_balance < min_balance_required:
                self.logger.warning(
                    f"余额不足 ({current_balance:.2f} < {min_balance_required})，"
                    f"跳过API请求和购买操作"
                )
                return

            min_price = invest_config.get("min_price", 100)
            if current_balance < min_price:
                self.logger.warning(f"余额不足 ({current_balance:.2f} < {min_price})，无法购买最低价商品，任务跳过")
                return
        except Exception as e:
            self.logger.error(f"获取余额失败: {e}")
            return

        # 2. 从白名单获取候选名单（仅使用白名单模式）
        self.logger.info("正在从白名单读取候选饰品（Scanner 智能选品）...")
        candidates = self.fetch_candidates_from_whitelist()

        if not candidates:
            self.logger.warning("未找到候选饰品，请先运行 Scanner.py 生成白名单")
            return
        
        # 打乱顺序（避免每次都从第1个开始）
        random.shuffle(candidates)
        
        # 每次运行只尝试前N个，防止频率限制
        max_try = invest_config.get("max_whitelist_try", 3)  # 每次最多尝试3个白名单饰品
        candidates = candidates[:max_try]
        self.logger.info(f">>> 从白名单获取到 {len(candidates)} 个候选饰品，已随机打乱顺序（狙击模式，每次最多尝试 {max_try} 个）<<<")

        # 3. 遍历并执行购买策略（狙击模式）
        invest_config = self.config.get("uu_auto_invest", {})
        max_orders = invest_config.get("max_orders_per_run", 5)  # 每次最多挂几个求购单
        buy_price_ratio = invest_config.get("buy_price_ratio", 0.90)  # 求购价 = 市场价 * 0.90
        
        # 拉长间隔到 20-40 秒（更保守，避免风控）
        min_interval = invest_config.get("interval_min", 20)  # 最小等待秒数（默认20秒）
        max_interval = invest_config.get("interval_max", 40)  # 最大等待秒数（默认40秒）
        
        success_count = 0
        busy_counter = 0  # 连续繁忙计数器（核心改动2：一击脱离）
        max_busy_count = 2  # 连续2次遇到系统繁忙就停止任务
        
        # 遍历候选列表
        for index, item in enumerate(candidates):
            # 检查今日购买上限
            if success_count >= max_orders:
                self.logger.info(f"已达到本次运行最大挂单数 ({max_orders})，停止任务")
                break

            # 连续风控检测
            if busy_counter >= max_busy_count:
                self.logger.error("!!! 连续触发风控，强制停止 !!!")
                break

            template_id = item["templateId"]
            item_name = item["name"]
            
            # === 修改点 1: 直接读取 Scanner 提供的白名单价格 (本地数据) ===
            # 不要在这里请求 get_item_details_from_uu !!!
            whitelist_price = item.get("market_price", 0)
            
            # 如果白名单里都没价格，直接跳过 (省一次请求)
            if whitelist_price <= 0:
                self.logger.debug(f"{item_name} 白名单中无市场价，跳过")
                continue

            # === 修改点 2: 本地预判 (先看钱够不够) ===
            # 这一步完全不需要联网，能挡掉很多无意义的请求
            if current_balance < whitelist_price:
                self.logger.warning(f"💰 余额不足买 {item_name} (需 {whitelist_price:.2f}元)，本地跳过")
                continue

            # === 修改点 3: 只有决定买了，才发起网络请求 (延迟确认) ===
            try:
                # 模拟一点延迟，不用太久，因为已经是"准下单"状态了
                sleep_time = random.uniform(min_interval, max_interval)
                self.logger.info(f"[{index+1}/{len(candidates)}] 正在核实 {item_name} ...")
                time.sleep(sleep_time)

                # =========== 【安全查价：使用 CSQAQ 替代 UU】 ===========
                
                # 1. 改为从 CSQAQ 获取实时数据 (不再请求 UU，避免风控)
                real_info = self._get_details_from_csqaq(template_id, item_name)
                
                if not real_info:
                    self.logger.warning(f"无法从 CSQAQ 获取 {item_name} 数据，跳过")
                    continue

                # 2. 提取数据
                real_time_lowest_price = real_info["lowest_sell_price"]
                market_hash_name = real_info["marketHashName"]  # HashName 也从这里拿

                # 3. 价格跳涨保护 (如果实时价比白名单贵 2%，放弃)
                if whitelist_price > 0 and real_time_lowest_price > whitelist_price * 1.02:
                    self.logger.warning(f"📉 {item_name} 价格跳涨 ({whitelist_price:.2f}->{real_time_lowest_price:.2f})，放弃")
                    continue
                    
                # 4. 实时余额检查
                if current_balance < real_time_lowest_price:
                    self.logger.warning(f"余额不足购买 {item_name} (需 {real_time_lowest_price:.2f}元)")
                    continue

                # 5. 准备下单数据
                target_price = real_time_lowest_price
                
                # 构建信号
                signal = {
                    "templateId": template_id,
                    "marketHashName": market_hash_name,
                    "name": item_name,
                    "market_price": real_time_lowest_price,
                    "target_price": target_price,
                    "roi": item.get("roi", 0),
                    "tier": item.get("tier", "Unknown"),
                    "timestamp": datetime.now().isoformat(),
                    "source": "UUAutoInvest_Sniper_V3",
                    "strategy_version": "v3_direct_sniper"
                }

                self.signal_manager.save_signal(signal)

                # 二次校验
                if not self.pre_trade_check(signal, current_balance):
                    continue

                # 执行下单
                self.logger.info(f"⚔️ 确认无误，发起购买 -> {item_name} | {target_price:.2f}元")
                try:
                    if self.executor.execute_buy(signal):
                        success_count += 1
                        current_balance -= target_price
                        self.logger.info("买到了，休息 60 秒...")
                        time.sleep(60)
                except Exception as buy_error:
                    # 如果是登录失效，停止整个任务
                    if "登录状态失效" in str(buy_error) or "84101" in str(buy_error):
                        self.logger.error("=" * 60)
                        self.logger.error("⛔ 检测到悠悠有品登录状态失效！")
                        self.logger.error("请重新登录悠悠有品并更新 Token")
                        self.logger.error("任务已停止，请修复登录问题后重新运行")
                        self.logger.error("=" * 60)
                        return  # 直接返回，停止整个任务
                    else:
                        # 其他错误继续处理下一个
                        self.logger.warning(f"下单失败: {buy_error}")
                        continue

            except Exception as e:
                # 如果是登录失效，停止整个任务
                if "登录状态失效" in str(e) or "84101" in str(e):
                    self.logger.error("=" * 60)
                    self.logger.error("⛔ 检测到悠悠有品登录状态失效！")
                    self.logger.error("请重新登录悠悠有品并更新 Token")
                    self.logger.error("任务已停止，请修复登录问题后重新运行")
                    self.logger.error("=" * 60)
                    return  # 直接返回，停止整个任务
                else:
                    handle_caught_exception(e, "UUAutoInvest")
                    self.logger.error(f"处理商品 {item_name} 时出错: {e}")
                    continue

        if busy_counter >= max_busy_count:
            self.logger.warning(f"本次任务因连续风控提前结束，成功挂单 {success_count} 个")
            self.logger.warning("建议：等待 30 分钟以上再重新运行脚本，让服务器重置IP权重")
        else:
            self.logger.info(f"本次任务结束，共成功挂单 {success_count} 个")

    def exec(self):
        """主执行函数"""
        invest_config = self.config.get("uu_auto_invest", {})
        if not invest_config.get("enable", False):
            return

        # 获取执行时间
        run_time = invest_config.get("run_time", "12:00")
        self.logger.info(f"自动投资插件已启动，将在每天 {run_time} 执行")

        # 启动时立即执行一次（可选）
        if invest_config.get("run_on_start", False):
            self.execute_investment()

        # 定时执行
        schedule.every().day.at(run_time).do(self.execute_investment)

        while True:
            if exit_code.get() != 0:
                break
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次


def main():
    """主函数 - 独立运行（用于单体测试）"""
    print("=" * 60)
    print("UUAutoInvest 模块单体测试")
    print("=" * 60)
    print("提示：")
    print("1. 确保 config.json5 中已配置 uu_auto_invest")
    print("2. 确保 config/whitelist.json 存在（由 Scanner.py 生成）")
    print("3. 确保 config/uu_token.txt 存在（悠悠有品登录 Token）")
    print("=" * 60)
    print()
    
    try:
        # 加载配置
        config_path = "config/config.json5"
        if not os.path.exists(config_path):
            print(f"❌ 配置文件不存在: {config_path}")
            return
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = json5.load(f)
        
        # 检查白名单文件
        whitelist_path = "config/whitelist.json"
        if not os.path.exists(whitelist_path):
            print(f"❌ 白名单文件不存在: {whitelist_path}")
            print("请先运行 Scanner.py 生成白名单")
            return
        
        # 检查 Token 文件
        token_path = "config/uu_token.txt"
        if not os.path.exists(token_path):
            print(f"❌ Token 文件不存在: {token_path}")
            print("请先登录悠悠有品并获取 Token")
            return
        
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        
        if not token:
            print("❌ Token 文件为空")
            return
        
        print(f"✅ 配置文件已加载")
        print(f"✅ 白名单文件: {whitelist_path}")
        print(f"✅ Token 文件: {token_path}")
        print()
        
        # 创建插件实例（独立运行模式，不需要 steam_client）
        # 注意：这里需要模拟一个 steam_client，但实际上只需要 uuyoupin
        class MockSteamClient:
            pass
        
        plugin = UUAutoInvest(MockSteamClient(), None, config)
        
        # 初始化 uuyoupin（使用文件中的 token）
        try:
            plugin.uuyoupin = uuyoupinapi.UUAccount(token)
            print("✅ 悠悠有品账户初始化成功")
        except Exception as e:
            print(f"❌ 悠悠有品账户初始化失败: {e}")
            return
        
        print()
        print("开始执行投资任务...")
        print()
        
        # 执行投资任务
        plugin.execute_investment()
        
        print()
        print("=" * 60)
        print("测试完成")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断测试")
    except Exception as e:
        print(f"\n\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

