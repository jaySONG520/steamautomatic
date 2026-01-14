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
            if res_data.get("Code") == 0:
                order_no = res_data.get("Data", {}).get("orderNo", "未知")
                self.logger.info(f"✅ 挂单成功！订单号: {order_no}")
                return True
            else:
                msg = res_data.get("Msg", "未知错误")
                self.logger.warning(f"❌ 挂单失败: {msg}")
                return False
                
        except Exception as e:
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
        从 Scanner.py 生成的白名单读取候选饰品列表
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

            for item in whitelist_data:
                template_id = str(item.get("templateId", ""))
                name = item.get("name", "未知")
                buy_limit = item.get("buy_limit", 0)  # Scanner.py 推荐的求购价
                yyyp_sell_price = item.get("yyyp_sell_price", 0)
                roi = item.get("roi", 0)

                if not template_id:
                    continue
                
                # 如果没有推荐价格，使用市场价的92%作为默认值
                if buy_limit <= 0 and yyyp_sell_price > 0:
                    buy_limit = round(yyyp_sell_price * 0.92, 2)

                if buy_limit <= 0:
                    continue

                candidates.append({
                    "templateId": template_id,
                    "name": name,
                    "market_price": yyyp_sell_price,
                    "target_buy_price": buy_limit,  # Scanner 推荐的求购价
                    "roi": roi,
                })

            self.logger.info(f"从白名单读取到 {len(candidates)} 个优质候选饰品")
            return candidates

        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error(f"读取白名单文件失败: {e}")
            return []


    def get_item_details_from_uu(self, template_id):
        """
        从悠悠有品获取饰品的详细信息（仅用于获取 marketHashName，不依赖价格）
        返回: (detail_dict, is_system_busy)
        """
        try:
            # 查询在售列表获取详情（只需要 marketHashName）
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
            # 只获取 marketHashName（用于挂单），不依赖价格
            market_hash_name = detail.get("commodityHashName") or detail.get("MarketHashName", "")
            
            if not market_hash_name:
                self.logger.warning(f"无法获取 marketHashName")
                return None, False
            
            return {
                "marketHashName": market_hash_name,
            }, False  # 成功时返回 False（不是系统繁忙）

        except Exception as e:
            self.logger.error(f"获取饰品 {template_id} 详情失败: {e}")
            return None

    def _get_csqaq_api_token(self):
        """获取 CSQAQ API Token"""
        if self._csqaq_api_token:
            return self._csqaq_api_token
        
        invest_config = self.config.get("uu_auto_invest", {})
        self._csqaq_api_token = invest_config.get("csqaq_api_token", "")
        return self._csqaq_api_token


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

    def execute_investment(self):
        """执行自动投资任务（重构版：信号驱动）"""
        self.logger.info(">>> 开始自动投资 (架构升级版) <<<")

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
        
        for index, item in enumerate(candidates):
            # 检查今日购买上限
            if success_count >= max_orders:
                self.logger.info(f"已达到本次运行最大挂单数 ({max_orders})，停止任务")
                break

            # === 核心改动2：连续风控自动停止（一击脱离）===
            # 如果连续2次遇到系统繁忙，直接放弃本次任务
            if busy_counter >= max_busy_count:
                self.logger.error("!!! 连续触发风控，强制停止本次任务，建议休息几小时后再来 !!!")
                self.logger.error("当前IP/账号可能已被标记，继续请求只会延长封禁时间")
                break
            # ========================

            template_id = item["templateId"]
            item_name = item["name"]

            try:
                # === 核心改良：随机延迟（模拟人类行为）===
                # 不要固定睡眠，随机睡眠可以让行为更像人类
                sleep_time = random.uniform(min_interval, max_interval)
                self.logger.info(f"[{index+1}/{len(candidates)}] 正在瞄准... 等待 {sleep_time:.1f} 秒")
                time.sleep(sleep_time)
                # ========================
                
                # 获取悠悠有品的实时详情
                detail, is_system_busy = self.get_item_details_from_uu(template_id)
                
                # === 核心改动2：一击脱离（遇到系统繁忙，小憩后继续，但计数）===
                if is_system_busy:
                    busy_counter += 1
                    self.logger.warning(f"系统繁忙 ({busy_counter}/{max_busy_count})，暂停 60 秒...")
                    time.sleep(60)  # 小憩一下，不连续请求
                    continue  # 跳过当前这个，继续下一个
                else:
                    busy_counter = 0  # 成功或者其他错误，重置繁忙计数
                # ========================
                
                if not detail:
                    self.logger.debug(f"无法获取 {item_name} 详情，跳过")
                    continue

                # 使用白名单中的商品名称和价格
                commodity_name = item_name  # 使用白名单中的名称
                market_hash_name = detail["marketHashName"]
                
                # 直接使用白名单中的市场价（更准确）
                lowest_price = item.get("market_price", 0)
                if lowest_price <= 0:
                    self.logger.warning(f"{item_name} 白名单中无市场价，跳过")
                    continue
                
                self.logger.info(f"{item_name} 市场价: {lowest_price:.2f}元 (来自白名单)")

                # 计算求购价：优先使用当前最高求购价+1元
                target_price = self._get_optimal_purchase_price(template_id, item_name, item.get("target_buy_price", 0), lowest_price)
                
                if target_price <= 0:
                    self.logger.warning(f"{item_name} 无法确定合适的求购价，跳过")
                    continue
                
                # === 步骤 C: 生成信号 (Signal Generation) ===
                # 构建信号对象
                signal = {
                    "templateId": template_id,
                    "marketHashName": market_hash_name,
                    "name": item_name,
                    "market_price": lowest_price,
                    "target_price": target_price,
                    "roi": item.get("roi", 0),
                    "tier": item.get("tier", "C"),  # 资产分级（如果有）
                    "timestamp": datetime.now().isoformat(),
                    "source": "UUAutoInvest",
                    "strategy_version": "v2_signal_separated"
                }
                
                # 信号落地（留痕）
                self.signal_manager.save_signal(signal)
                
                # === 步骤 D: 二次校验 (Pre-Trade Check) ===
                # 这一步是把关的最后一道防线
                if not self.pre_trade_check(signal, current_balance):
                    continue
                
                # === 步骤 E: 交给执行器 (Execution) ===
                if self.executor.execute_buy(signal):
                    success_count += 1
                    current_balance -= target_price  # 更新本地余额缓存
                    self.logger.info("买到了，贤者模式 60 秒...")
                    time.sleep(60)

            except Exception as e:
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

