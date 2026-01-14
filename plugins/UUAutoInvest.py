import json
import os
import re
import time
import random  # 新增：用于随机延迟，模拟人类行为
from datetime import datetime, timedelta

import json5
import requests
import schedule

import uuyoupinapi
from utils.logger import PluginLogger, handle_caught_exception
from utils.tools import exit_code
from utils.uu_helper import get_valid_token_for_uu


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
        # API session（用于保持 cookie）
        self._api_session = None

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
            self.logger.info("自动投资插件初始化成功")
            return False
        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error("自动投资插件初始化失败")
            return True

    def fetch_candidates_from_whitelist(self):
        """
        从选品器生成的白名单读取候选饰品列表
        支持两种格式：
        1. Scanner.py 生成的 whitelist.json（简化格式，直接是数组）
        2. Hunter.py 生成的 invest_whitelist.json（完整格式，包含 metadata）
        """
        candidates = []
        
        # 优先级：Scanner.py 的 whitelist.json > Hunter.py 的 invest_whitelist.json
        scanner_whitelist = "config/whitelist.json"
        hunter_whitelist = "config/invest_whitelist.json"
        
        whitelist_file = None
        if os.path.exists(scanner_whitelist):
            whitelist_file = scanner_whitelist
            self.logger.info("检测到 Scanner.py 生成的白名单，优先使用")
        elif os.path.exists(hunter_whitelist):
            whitelist_file = hunter_whitelist
            self.logger.info("检测到 Hunter.py 生成的白名单")
        else:
            self.logger.debug("未找到白名单文件，请先运行 Scanner.py 或 Hunter.py")
            return []

        try:
            with open(whitelist_file, "r", encoding="utf-8") as f:
                whitelist_data = json5.load(f)

            items = []
            generated_at = "未知时间"
            
            # 判断格式：如果是数组，说明是 Scanner.py 生成的简化格式
            if isinstance(whitelist_data, list):
                items = whitelist_data
                self.logger.info(f"从 Scanner 白名单读取候选饰品（共 {len(items)} 个）")
            # 如果是字典，说明是 Hunter.py 生成的完整格式
            elif isinstance(whitelist_data, dict):
                items = whitelist_data.get("items", [])
                generated_at = whitelist_data.get("generated_at", "未知时间")
                self.logger.info(f"从 Hunter 白名单读取候选饰品（生成时间: {generated_at}，共 {len(items)} 个）")
            else:
                self.logger.warning("白名单文件格式错误")
                return []

            if not items:
                self.logger.warning("白名单为空")
                return []

            for item in items:
                # 兼容两种格式的字段名
                template_id = str(item.get("templateId") or item.get("id", ""))
                good_id = item.get("good_id") or template_id
                name = item.get("name", "未知")
                
                # Scanner.py 使用 buy_limit，Hunter.py 使用 target_buy_price
                target_price = item.get("buy_limit") or item.get("target_buy_price", 0)
                yyyp_sell_price = item.get("yyyp_sell_price", 0)
                roi = item.get("roi", 0)
                stability_score = item.get("stability_score", 0)
                volatility = item.get("volatility", 0)

                if not template_id:
                    continue
                
                # 如果没有推荐价格，使用市场价的92%作为默认值
                if target_price <= 0 and yyyp_sell_price > 0:
                    target_price = round(yyyp_sell_price * 0.92, 2)

                if target_price <= 0:
                    continue

                candidates.append({
                    "templateId": template_id,
                    "good_id": good_id,
                    "name": name,
                    "market_price": yyyp_sell_price,
                    "target_buy_price": target_price,  # 选品器推荐的求购价
                    "roi": roi,
                    "stability_score": stability_score,
                    "volatility": volatility,
                    "from_whitelist": True,  # 标记来自白名单
                })

            self.logger.info(f"从白名单读取到 {len(candidates)} 个优质候选饰品")
            return candidates

        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error(f"读取白名单文件失败: {e}")
            return []

    def fetch_candidates_from_file(self):
        """
        从本地文件读取候选饰品列表（兼容旧格式）
        用户需要手动从第三方网站（如 csqaq.com/rank）获取数据并保存为 JSON 文件
        """
        candidates = []
        config_folder = "config"
        candidates_file = os.path.join(config_folder, "invest_candidates.json")

        if not os.path.exists(candidates_file):
            self.logger.debug(f"未找到候选饰品文件: {candidates_file}")
            return []

        try:
            with open(candidates_file, "r", encoding="utf-8") as f:
                data = json5.load(f)

            invest_config = self.config.get("uu_auto_invest", {})
            min_price = invest_config.get("min_price", 100)
            max_price = invest_config.get("max_price", 2000)
            min_roi = invest_config.get("min_roi", 0.25)  # 25% 年化收益率

            # 解析数据（根据实际 JSON 结构调整）
            # 假设数据结构是 list，每个 item 有 templateId, price, rent 等字段
            data_list = data if isinstance(data, list) else data.get("list", [])

            for item in data_list:
                # 根据实际 JSON 结构调整字段名
                t_id = item.get("templateId") or item.get("template_id") or item.get("id")
                price = float(item.get("price", 0) or item.get("market_price", 0))
                rent = float(item.get("rent", 0) or item.get("daily_rent", 0) or item.get("rent_price", 0))
                name = item.get("name") or item.get("commodity_name") or "未知"

                if not t_id or price <= 0 or rent <= 0:
                    continue

                # 价格区间筛选
                if not (min_price <= price <= max_price):
                    continue

                # 计算年化收益率 (日租金 * 365 / 价格)
                roi = (rent * 365) / price if price > 0 else 0
                if roi >= min_roi:
                    candidates.append({
                        "templateId": str(t_id),
                        "name": name,
                        "market_price": price,
                        "daily_rent": rent,
                        "roi": roi,
                    })

            self.logger.info(f"从文件筛选出 {len(candidates)} 个符合年化 > {min_roi*100}% 的候选饰品")
            return candidates

        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error(f"读取候选饰品文件失败: {e}")
            return []

    def _get_api_token(self):
        """
        获取 CSQAQ API Token
        优先从配置中读取，如果没有则返回 None
        """
        invest_config = self.config.get("uu_auto_invest", {})
        api_token = invest_config.get("csqaq_api_token", "")
        
        if api_token:
            return api_token
        
        # 如果配置中没有，尝试从旧的 authorization 配置中读取（兼容旧配置）
        old_auth = invest_config.get("csqaq_authorization", "")
        if old_auth:
            self.logger.info("检测到旧的 csqaq_authorization 配置，将使用它作为 ApiToken")
            return old_auth
        
        return None

    def _get_authorization_auto(self):
        """
        自动获取 csqaq.com 的 Authorization
        策略：先访问页面建立 session，然后尝试不带 Authorization 的请求
        如果失败，尝试从页面 JavaScript 中提取
        """
        try:
            self.logger.info("正在自动获取 Authorization...")
            
            # 创建 session 以保持 cookie
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://csqaq.com/",
            })
            
            # 方法1: 先访问 rank 页面建立 session
            rank_url = "https://csqaq.com/rank"
            resp = session.get(rank_url, timeout=10)
            resp.raise_for_status()
            
            # 方法2: 尝试从页面 HTML/JavaScript 中提取 Authorization
            html_content = resp.text
            
            # 查找可能的 Authorization 模式（可能在 JavaScript 变量或配置中）
            auth_patterns = [
                r'Authorization["\']?\s*[:=]\s*["\']([^"\']{30,})["\']',  # 至少30个字符
                r'authorization["\']?\s*[:=]\s*["\']([^"\']{30,})["\']',
                r'["\']Authorization["\']:\s*["\']([^"\']{30,})["\']',
                r'auth["\']?\s*[:=]\s*["\']([^"\']{30,})["\']',
                r'token["\']?\s*[:=]\s*["\']([^"\']{30,})["\']',
            ]
            
            for pattern in auth_patterns:
                matches = re.findall(pattern, html_content, re.IGNORECASE)
                if matches:
                    auth = matches[0].strip()
                    # 验证格式（通常包含字母数字和连字符）
                    if re.match(r'^[a-zA-Z0-9\-_]+$', auth) and len(auth) > 30:
                        self.logger.info(f"从页面中提取到 Authorization（长度: {len(auth)}）")
                        return auth
            
            # 方法3: 尝试不带 Authorization 直接调用 API（可能只需要 cookie）
            test_api_url = "https://csqaq.com/proxies/api/v1/info/get_rank_list"
            test_headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://csqaq.com",
                "Referer": "https://csqaq.com/rank",
            }
            test_data = {"page_index": 1, "page_size": 1}
            
            test_resp = session.post(test_api_url, headers=test_headers, json=test_data, timeout=10)
            
            # 如果请求成功，说明不需要 Authorization（可能只需要 cookie）
            if test_resp.status_code == 200:
                try:
                    result = test_resp.json()
                    if result.get("code") == 200:
                        self.logger.info("API 请求成功，可能不需要 Authorization（使用 session cookie）")
                        # 保存 session 供后续使用
                        self._api_session = session
                        return ""  # 返回空字符串，表示不需要 Authorization
                except:
                    pass
            
            # 如果都失败，返回 None
            self.logger.warning("无法自动获取 Authorization，建议手动配置或使用文件模式")
            return None
            
        except Exception as e:
            self.logger.error(f"自动获取 Authorization 失败: {e}")
            return None

    def fetch_candidates_from_api(self):
        """
        从 CSQAQ 官方 API 获取候选饰品列表
        使用官方 API: https://api.csqaq.com/api/v1
        """
        candidates = []
        invest_config = self.config.get("uu_auto_invest", {})
        
        # 使用官方 API 地址
        api_url = "https://api.csqaq.com/api/v1/info/get_rank_list"
        
        # 获取 API Token
        api_token = self._get_api_token()
        
        if not api_token:
            self.logger.warning("未配置 csqaq_api_token，无法使用API获取数据")
            self.logger.info("请在 config.json5 中配置 csqaq_api_token（从 csqaq.com 用户中心获取）")
            return []

        try:
            # 创建 session
            session = requests.Session()
            
            # 使用官方 API 的认证方式：ApiToken Header
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "ApiToken": api_token,  # 官方 API 使用 ApiToken 而不是 Authorization
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            # 构建请求体（根据官方 API 文档）
            # filter 参数需要参考详细文档，这里使用基本筛选
            page_size = min(invest_config.get("api_page_size", 100), 500)  # 增加每页数量，最大500
            request_data = {
                "page_index": 1,
                "page_size": page_size,
                "search": "",
                "filter": {
                    # 根据官方文档，filter 参数较多，这里使用基本筛选
                    # 详细参数请参考：http://docs.csqaq.com/doc-4619235
                    "排序": ["租赁_短租收益率(年化)"],  # 按年化收益率排序
                    # 添加价格筛选，减少无效数据
                    "价格最低价": invest_config.get("min_price", 100),
                    "价格最高价": invest_config.get("max_price", 2000),
                    # 添加在售数量筛选
                    "在售最少": invest_config.get("min_on_sale", 50),  # 降低到20，让API先过滤
                },
                "show_recently_price": invest_config.get("show_recently_price", False)
            }

            self.logger.info("正在从 CSQAQ 官方 API 获取候选饰品列表...")
            
            # 遵守频率限制：1次/秒
            time.sleep(1)
            
            resp = session.post(api_url, headers=headers, json=request_data, timeout=15)
            
            # 先检查HTTP状态码
            if resp.status_code == 401:
                self.logger.error("API返回401未授权错误，可能的原因：")
                self.logger.error("1. csqaq_api_token 配置错误或已过期")
                self.logger.error("2. IP地址未绑定到API白名单（需要在 csqaq.com 用户中心绑定IP）")
                self.logger.error("3. 请检查 config.json5 中的 csqaq_api_token 是否正确")
                try:
                    error_detail = resp.json()
                    self.logger.debug(f"API错误详情: {error_detail}")
                except:
                    self.logger.debug(f"HTTP响应内容: {resp.text[:200]}")
                return []
            
            # 检查其他HTTP错误
            if resp.status_code != 200:
                self.logger.error(f"API请求失败: HTTP {resp.status_code} - {resp.reason}")
                try:
                    error_detail = resp.json()
                    self.logger.debug(f"API错误详情: {error_detail}")
                except:
                    self.logger.debug(f"HTTP响应内容: {resp.text[:200]}")
                return []
            
            result = resp.json()
            
            self.logger.info("API请求成功")

            # 检查返回码（官方 API 状态码）
            code = result.get("code")
            if code not in [200, 201]:  # 2xx 表示成功
                msg = result.get("msg", "未知错误")
                if code == 400:
                    self.logger.error(f"API返回错误: 用户不存在或Token验证未通过 (code: {code})")
                elif code == 401:
                    self.logger.error(f"API返回错误: Token验证未通过，请检查 csqaq_api_token 是否正确 (code: {code})")
                    self.logger.error("提示：请确保已在 csqaq.com 用户中心绑定IP白名单")
                elif code == 429:
                    self.logger.error(f"API返回错误: 请求过于频繁，请稍后再试 (code: {code})")
                elif code == 503:
                    self.logger.error(f"API返回错误: 网关异常或请求频繁 (code: {code})")
                else:
                    self.logger.error(f"API返回错误: {msg} (code: {code})")
                return []

            data_list = result.get("data", {}).get("data", [])
            if not data_list:
                self.logger.warning("API返回数据为空")
                return []

            self.logger.info(f"API返回了 {len(data_list)} 条数据，开始筛选...")
            # 打印前3条数据用于调试
            if data_list:
                self.logger.debug(f"前3条数据示例: {json.dumps(data_list[:3], ensure_ascii=False, indent=2)}")

            # 筛选条件
            min_price = invest_config.get("min_price", 100)
            max_price = invest_config.get("max_price", 2000)
            min_roi = invest_config.get("min_roi", 0.25)  # 25% 年化收益率
            min_on_sale = invest_config.get("min_on_sale", 50)

            # 由于API返回的数据是按年化收益率降序排列的，如果当前项已经低于min_roi，后续的肯定也低于
            # 可以提前停止处理，提高效率
            early_stop = False
            
            for item in data_list:
                # 如果已经遇到年化收益率低于阈值的项，提前停止（因为数据是按年化收益率降序排列的）
                if early_stop:
                    break
                
                # 解析官方 API 返回的数据（根据文档）
                item_id = item.get("id")
                name = item.get("name", "未知")
                
                # 悠悠有品售价（API返回的是小数，单位：元）
                yyyp_sell_price = float(item.get("yyyp_sell_price", 0))
                
                # 悠悠有品求购价
                yyyp_buy_price = float(item.get("yyyp_buy_price", 0))
                
                # 悠悠有品短租价格（日租金）
                yyyp_lease_price = float(item.get("yyyp_lease_price", 0))
                
                # 年化收益率（API直接返回，单位：百分比，如79.9表示79.9%）
                yyyp_lease_annual = item.get("yyyp_lease_annual", 0)
                if yyyp_lease_annual:
                    # API返回的是百分比，转换为小数（79.9 -> 0.799）
                    roi = float(yyyp_lease_annual) / 100.0
                else:
                    # 如果没有年化收益率，根据日租金计算
                    price = yyyp_sell_price if yyyp_sell_price > 0 else yyyp_buy_price
                    if yyyp_lease_price > 0 and price > 0:
                        roi = (yyyp_lease_price * 365) / price
                    else:
                        roi = 0
                
                # 在售数量（根据文档字段名）
                yyyp_sell_num = item.get("yyyp_sell_num", 0)
                
                # 使用售价作为价格参考
                price = yyyp_sell_price if yyyp_sell_price > 0 else yyyp_buy_price

                if not item_id:
                    continue
                
                if price <= 0:
                    self.logger.debug(f"跳过 {name}：价格为0 (售价:{yyyp_sell_price}, 求购价:{yyyp_buy_price})")
                    continue

                # 价格区间筛选
                if not (min_price <= price <= max_price):
                    self.logger.debug(f"跳过 {name}：价格 {price:.2f} 不在区间 [{min_price}, {max_price}]")
                    continue

                # 年化收益率筛选（如果低于阈值，标记提前停止）
                if roi < min_roi:
                    self.logger.debug(
                        f"跳过 {name}：年化收益率 {roi*100:.1f}% < {min_roi*100}% "
                        f"(API返回:{yyyp_lease_annual}%, 日租金:{yyyp_lease_price:.2f}, 售价:{price:.2f})"
                    )
                    # 由于数据是按年化收益率降序排列的，后续数据肯定也低于阈值，提前停止
                    early_stop = True
                    self.logger.info(f"年化收益率已低于 {min_roi*100}%，提前停止处理后续数据")
                    break

                # 在售数量筛选（确保有足够的流动性）
                if yyyp_sell_num < min_on_sale:
                    self.logger.debug(f"跳过 {name}：在售数量 {yyyp_sell_num} < {min_on_sale}")
                    continue

                self.logger.info(
                    f"找到候选饰品: {name}, 价格: {price:.2f}, "
                    f"日租金: {yyyp_lease_price:.2f}, 年化: {roi*100:.1f}% (API:{yyyp_lease_annual}%), 在售: {yyyp_sell_num}"
                )

                candidates.append({
                    "templateId": str(item_id),
                    "name": name,
                    "market_price": price,
                    "yyyp_sell_price": yyyp_sell_price,
                    "yyyp_buy_price": yyyp_buy_price,
                    "yyyp_lease_price": yyyp_lease_price,
                    "roi": roi,
                    "yyyp_sell_num": yyyp_sell_num,
                })

            self.logger.info(f"从API筛选出 {len(candidates)} 个符合年化 > {min_roi*100}% 的候选饰品")
            return candidates

        except requests.exceptions.RequestException as e:
            self.logger.error(f"API请求失败: {e}")
            return []
        except Exception as e:
            handle_caught_exception(e, "UUAutoInvest")
            self.logger.error(f"解析API响应失败: {e}")
            return []

    def get_item_details_from_uu(self, template_id):
        """
        从悠悠有品获取饰品的详细信息（用于挂求购单）
        返回: (detail_dict, is_system_busy)
        """
        try:
            # 查询在售列表获取详情
            res = self.uuyoupin.get_market_sale_list_with_abrade(
                int(template_id), pageIndex=1, pageSize=1
            )
            
            # 处理 HTTP 层面错误（429 Too Many Requests）
            # call_api 返回的是 requests.Response 对象
            if isinstance(res, requests.Response):
                if res.status_code == 429:
                    self.logger.warning("HTTP 429: 请求过于频繁")
                    return None, True  # True 表示系统繁忙
                market_data = res.json()
            else:
                # 如果不是 Response 对象，尝试直接作为 JSON 处理（兼容性）
                market_data = res if isinstance(res, dict) else res.json()

            # 兼容大小写：Code 或 code
            code = market_data.get("Code")
            if code is None:
                code = market_data.get("code", -1)
            
            msg = market_data.get("Msg") or market_data.get("msg", "未知错误")
            
            # 判定系统繁忙的条件（更精准的识别）
            is_busy = (
                code == 84104 or  # 悠悠有品特定的频繁请求错误码
                code == 429 or    # HTTP 429
                "频繁" in msg or 
                "系统繁忙" in msg or
                code == -1  # 系统繁忙时通常返回 -1
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
            # 兼容不同的字段名
            commodity_name = detail.get("commodityName") or detail.get("CommodityName", "")
            market_hash_name = detail.get("commodityHashName") or detail.get("MarketHashName", "")
            price_str = detail.get("price") or detail.get("Price", "0")
            
            return {
                "commodityName": commodity_name,
                "marketHashName": market_hash_name,
                "lowestPrice": float(price_str),
            }, False  # 成功时返回 False（不是系统繁忙）

        except Exception as e:
            self.logger.error(f"获取饰品 {template_id} 详情失败: {e}")
            return None

    def execute_investment(self):
        """执行自动投资任务（狙击模式）"""
        self.logger.info(">>> 开始自动投资 (狙击模式) <<<")

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

        # 2. 获取候选名单（优先级：白名单 > API > 文件）
        candidates = []
        use_whitelist = invest_config.get("use_whitelist", True)  # 默认优先使用白名单
        use_api = invest_config.get("use_api", True)
        
        # 优先使用选品器生成的白名单（Scanner.py 或 Hunter.py）
        if use_whitelist:
            self.logger.info("正在从白名单读取候选饰品（Scanner/Hunter 智能选品）...")
            candidates = self.fetch_candidates_from_whitelist()
        
        # 如果白名单为空，尝试使用 API
        if not candidates and use_api:
            self.logger.info("白名单为空，正在使用API方式获取候选饰品...")
            candidates = self.fetch_candidates_from_api()
            # 如果API获取失败，尝试使用文件
            if not candidates:
                self.logger.info("API获取失败，尝试从文件读取...")
                candidates = self.fetch_candidates_from_file()
        elif not candidates:
            # 如果禁用白名单且禁用API，使用文件
            self.logger.info("正在使用文件方式获取候选饰品...")
            candidates = self.fetch_candidates_from_file()

        if not candidates:
            self.logger.warning("未找到符合条件的候选饰品")
            return
        
        # === 核心改动1：打乱顺序（避免每次都从第1个开始）===
        # 避免每次都从第1个开始请求，防止死磕同一个坏数据
        random.shuffle(candidates)
        
        # 如果使用白名单，每次运行只尝试前3个，防止频率限制
        if candidates and candidates[0].get("from_whitelist"):
            max_try = invest_config.get("max_whitelist_try", 3)  # 每次最多尝试3个白名单饰品
            candidates = candidates[:max_try]
            self.logger.info(f">>> 从白名单获取到 {len(candidates)} 个候选饰品，已随机打乱顺序（狙击模式，每次最多尝试 {max_try} 个）<<<")
        else:
            self.logger.info(f">>> 获取到 {len(candidates)} 个候选饰品，已随机打乱顺序（狙击模式）<<<")

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

                commodity_name = detail["commodityName"]
                market_hash_name = detail["marketHashName"]
                lowest_price = detail["lowestPrice"]

                # 计算求购价：优先使用白名单推荐价格
                if item.get("from_whitelist"):
                    # 优先使用白名单推荐的求购价（Scanner/Hunter 已经经过严格筛选）
                    buy_limit = item.get("buy_limit")  # Scanner.py 格式
                    target_buy_price = item.get("target_buy_price")  # Hunter.py 格式
                    recommended_price = buy_limit or target_buy_price
                    
                    if recommended_price and recommended_price > 0:
                        target_price = recommended_price
                        self.logger.info(f"{item_name} 使用白名单推荐求购价: {target_price:.2f} (市场价: {lowest_price:.2f})")
                    else:
                        # 如果白名单没有推荐价格，使用市场价的92%
                        target_price = round(lowest_price * 0.92, 2)
                        self.logger.info(f"{item_name} 使用默认求购价（市场价92%）: {target_price:.2f} (市场价: {lowest_price:.2f})")
                else:
                    # 否则使用配置的比例计算
                    target_price = round(lowest_price * buy_price_ratio, 2)
                    
                    # 如果API提供了求购价参考，使用更保守的价格
                    if "yyyp_buy_price" in item and item["yyyp_buy_price"] > 0:
                        # 如果计算出的求购价高于API提供的求购价，使用API的求购价（更保守）
                        if target_price > item["yyyp_buy_price"]:
                            target_price = round(item["yyyp_buy_price"] * 0.98, 2)
                            self.logger.debug(f"{item_name} 使用API求购价参考: {target_price:.2f}")
                
                # 验证求购价必须低于市场价
                if target_price >= lowest_price:
                    self.logger.warning(f"{item_name} 计算出的求购价 {target_price:.2f} >= 市场最低价 {lowest_price:.2f}，跳过（不合理）")
                    continue

                # 再次校验价格区间
                min_price = invest_config.get("min_price", 100)
                max_price = invest_config.get("max_price", 2000)
                if not (min_price <= target_price <= max_price):
                    self.logger.debug(f"{item_name} 求购价 {target_price} 不在价格区间内，跳过")
                    continue

                # 4. 余额检查
                if current_balance < target_price:
                    self.logger.info(f"余额不足购买 {item_name} (需 {target_price:.2f}，当前余额 {current_balance:.2f})，跳过")
                    continue

                # 5. 执行挂单
                test_mode = invest_config.get("test_mode", False)
                
                try:
                    self.logger.info(f"正在挂单 -> {commodity_name} | 价格: {target_price:.2f}, 市场价: {lowest_price:.2f}, 年化: {item['roi']*100:.1f}%")
                    
                    # 如果是测试模式，不真挂单
                    if test_mode:
                        self.logger.info("[测试模式] 挂单请求已模拟发送")
                        success_count += 1
                        current_balance -= target_price  # 模拟扣减
                        # 挂单成功后，休息更久一点，模拟人类喜悦
                        self.logger.info("买到了，休息 60 秒...")
                        time.sleep(60)
                        continue
                    
                    # 实际挂单
                    self.logger.info(f"发起挂单 -> {commodity_name} | 价格: {target_price:.2f}")
                    res = self.uuyoupin.publish_purchase_order(
                        templateId=int(template_id),
                        templateHashName=market_hash_name,
                        commodityName=commodity_name,
                        purchasePrice=target_price,
                        purchaseNum=1
                    )
                    
                    res_data = res.json()
                    if res_data.get("Code") == 0:
                        order_no = res_data.get("Data", {}).get("orderNo", "未知")
                        self.logger.info(f"✅ 挂单成功！订单号: {order_no}")
                        current_balance -= target_price  # 扣减本地余额
                        success_count += 1
                        # 挂单成功后，休息更久一点，模拟人类喜悦
                        self.logger.info("买到了，休息 60 秒...")
                        time.sleep(60)
                    else:
                        msg = res_data.get("Msg", "未知错误")
                        self.logger.warning(f"❌ 挂单失败: {msg}")
                        
                except Exception as e:
                    self.logger.error(f"挂单异常: {e}")
                    handle_caught_exception(e, "UUAutoInvest")

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

