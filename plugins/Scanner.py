"""
CSQAQ 智能选品扫描器 (Scanner)
三期过滤法：从高回报榜单中筛选出真正的理财枪皮
建议每天中午 12:00 或晚上 20:00 运行一次
"""

import json
import os
import sys
import time
import random
from typing import Optional, List, Dict
from datetime import datetime

# 添加项目根目录到 Python 路径（用于独立运行）
if __name__ == "__main__":
    # 获取当前文件所在目录的父目录（项目根目录）
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json5
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from utils.logger import PluginLogger, handle_caught_exception


class CSQAQScanner:
    """
    三期过滤法选品器
    1. 回报率初筛：年化收益率在合理区间
    2. 趋势初筛：90天不跌超过10%
    3. 详情深挖：获取在租数量等热度指标
    4. 稳定性终审：90天价格波动低于15%
    """

    def __init__(self, config_path: str = "config/config.json5"):
        self.logger = PluginLogger("Scanner")
        self.config_path = config_path
        self.config = self._load_config()
        
        # 从配置读取参数
        invest_config = self.config.get("uu_auto_invest", {})
        
        # 选品硬指标配置（严选模式）
        scanner_config = self.config.get("scanner", {})
        
        # === 核心门槛配置 (拒绝垃圾饰品) ===
        self.MIN_PRICE = scanner_config.get("min_price_hard", 200.0)  # 价格硬门槛：200元（低于这个不看）
        self.MIN_DAILY_RENT = scanner_config.get("min_daily_rent", 0.5)  # 日租金底线：0.5元（0.3元那种没肉吃）
        self.MIN_LEASE_COUNT = scanner_config.get("min_lease_count", 30)  # 最小在租人数：30人（少于这个说明根本没人租）
        self.MIN_LEASE_RATIO = scanner_config.get("min_lease_ratio", 0.15)  # 最小出租率：15%（在租/在售，防止库存积压）
        
        # 其他配置
        self.MAX_PRICE = invest_config.get("max_price", 2000)  # 价格上限
        self.MAX_VOLATILITY = scanner_config.get("max_lease_volatility", 0.25)  # 最大租金波动率 25%
        
        # API 配置
        self.api_token = self._get_api_token()
        self.base_url = "https://api.csqaq.com/api/v1"
        self.headers = {
            "ApiToken": self.api_token,
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # === 代理配置（用于固定出口IP，解决VPN IP变化问题）===
        # 从配置文件读取代理设置，如果没有配置则使用默认值
        scanner_config = self.config.get("scanner", {})
        proxy_config = scanner_config.get("proxy", {})
        
        # 默认代理端口（常见VPN软件的默认端口）
        # Clash: 7890, v2rayN: 10809 (HTTP) 或 10808 (SOCKS)
        default_proxy_port = proxy_config.get("port", 7890)
        proxy_enabled = proxy_config.get("enable", False)
        
        self.proxies = None
        if proxy_enabled:
            proxy_host = proxy_config.get("host", "127.0.0.1")
            proxy_type = proxy_config.get("type", "http")  # http 或 socks5
            
            if proxy_type == "socks5":
                # SOCKS5 代理需要使用 socks 协议
                try:
                    import socks
                    from urllib3.contrib.socks import SOCKSProxyManager
                    self.proxies = {
                        "http": f"socks5://{proxy_host}:{default_proxy_port}",
                        "https": f"socks5://{proxy_host}:{default_proxy_port}"
                    }
                except ImportError:
                    self.logger.warning("未安装 socks 支持库，SOCKS5 代理不可用，请安装: pip install pysocks")
                    self.proxies = None
            else:
                # HTTP 代理
                self.proxies = {
                    "http": f"http://{proxy_host}:{default_proxy_port}",
                    "https": f"http://{proxy_host}:{default_proxy_port}"
                }
            
            if self.proxies:
                self.logger.info(f"✅ 已启用固定代理: {proxy_type}://{proxy_host}:{default_proxy_port} (用于固定出口IP)")
            else:
                self.logger.warning("⚠️ 代理配置无效，将使用直连模式")
        else:
            self.logger.debug("未启用代理，使用直连模式（如果VPN导致IP变化，建议启用代理）")
        
        # 配置重试机制，解决网络不稳
        self.session = requests.Session()
        
        # 应用代理配置（如果启用）
        if self.proxies:
            self.session.proxies.update(self.proxies)
            
            # === 测试代理是否生效 ===
            try:
                self.logger.info("正在测试代理连接...")
                # 访问一个查IP的网站
                test_resp = self.session.get("http://httpbin.org/ip", timeout=10, verify=False)
                if test_resp.status_code == 200:
                    test_ip = test_resp.json().get('origin', '未知')
                    self.logger.info(f"✅ 代理生效! 当前出口IP: {test_ip}")
                    # 记录测试IP，用于后续对比
                    self.test_proxy_ip = test_ip
                else:
                    self.logger.warning(f"⚠️ 代理测试失败: HTTP {test_resp.status_code}")
            except Exception as e:
                self.logger.error(f"❌ 代理测试失败: {e} (请检查端口或节点是否正常)")
                self.logger.warning("⚠️ 代理可能未生效，将使用直连模式（可能导致IP变化问题）")
        else:
            self.test_proxy_ip = None
        
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.headers.update(self.headers)
        
        # 记录上次绑定IP时间，防止频繁请求触发429
        self.last_bind_time = 0
        self.last_bind_ip = None  # 记录上次绑定的IP地址（用于检测VPN IP变化）
        
        # 输出文件
        self.whitelist_path = "config/whitelist.json"
        
        if not self.api_token:
            self.logger.warning("未配置 csqaq_api_token，Scanner 无法运行")
            self.logger.info("请在 config.json5 中配置 csqaq_api_token（从 csqaq.com 用户中心获取）")

    def _load_config(self) -> dict:
        """加载配置文件"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json5.load(f)
        except Exception as e:
            self.logger.error(f"加载配置文件失败: {e}")
            return {}

    def _get_api_token(self) -> str:
        """获取 CSQAQ API Token"""
        invest_config = self.config.get("uu_auto_invest", {})
        return invest_config.get("csqaq_api_token", "") or invest_config.get("csqaq_authorization", "")

    def _extract_ip_from_response(self, data: str) -> Optional[str]:
        """
        从绑定IP的响应中提取当前IP地址
        例如："绑定IP更新成功，当前绑定IP为：102.114.14.120"
        """
        try:
            if "当前绑定IP为：" in data:
                ip = data.split("当前绑定IP为：")[1].strip()
                return ip
            return None
        except:
            return None

    def _get_current_ip_from_response(self, data: str) -> Optional[str]:
        """
        从绑定IP的响应中提取当前IP地址
        例如："绑定IP更新成功，当前绑定IP为：102.114.14.120"
        """
        try:
            if "当前绑定IP为：" in data:
                ip = data.split("当前绑定IP为：")[1].strip()
                return ip
            return None
        except:
            return None

    def bind_local_ip(self, force: bool = False) -> bool:
        """
        绑定本机白名单IP (带冷却保护和IP变化检测)
        为当前请求的API_TOKEN绑定本机的IP地址，适用于非固定IP场景下使用（如VPN）
        频率限制：30秒/次
        :param force: 是否强制绑定（忽略冷却时间，用于401错误时或IP变化时）
        :return: 是否绑定成功
        """
        if not self.api_token:
            self.logger.warning("未配置 API Token，无法绑定IP")
            return False

        # 冷却检查：30秒内不重复绑定（除非强制）
        now = time.time()
        if not force and now - self.last_bind_time < 35:
            self.logger.debug("IP绑定处于冷却中，跳过本次绑定请求")
            return True

        url = f"{self.base_url}/sys/bind_local_ip"
        
        try:
            self.logger.info("正在维护API白名单(绑定本机IP)...")
            
            resp = self.session.post(url, headers=self.headers, timeout=10, verify=False)
            
            # 处理 429 Too Many Requests
            if resp.status_code == 429:
                self.logger.warning("绑定IP频率过快(HTTP 429)，视为成功，继续运行")
                self.last_bind_time = now  # 更新时间，避免立即重试
                return True
            
            if resp.status_code != 200:
                self.logger.error(f"绑定IP失败: HTTP {resp.status_code}")
                return False
            
            result = resp.json()
            code = result.get("code")
            msg = result.get("msg", "")
            data = result.get("data", "")
            
            if code == 200:
                self.last_bind_time = now
                # 提取并记录当前绑定的IP
                current_ip = self._get_current_ip_from_response(data)
                if current_ip:
                    if self.last_bind_ip and self.last_bind_ip != current_ip:
                        self.logger.warning(f"⚠️ 检测到IP变化: {self.last_bind_ip} -> {current_ip} (可能是VPN切换)")
                    self.last_bind_ip = current_ip
                self.logger.info(f"✅ {data}")
                return True
            elif code == 429:
                self.logger.warning(f"⚠️ 请求频率过快，绑定IP频率限制为30秒/次。{data}")
                self.last_bind_time = now  # 更新时间，避免立即重试
                # 即使频率限制，也返回True，因为可能是刚刚绑定过
                return True
            else:
                self.logger.error(f"绑定IP失败: {msg} (code: {code})")
                if data:
                    self.logger.error(f"详情: {data}")
                return False
                
        except Exception as e:
            self.logger.error(f"绑定IP异常: {e}")
            return False

    def get_rank_list(self, filter_payload: dict) -> List[dict]:
        """
        通用排行榜请求（支持不同筛选策略）
        优化：多页请求，扩大样本量
        :param filter_payload: filter 字典
        :return: 饰品列表
        """
        url = f"{self.base_url}/info/get_rank_list"
        
        all_items = []
        max_pages = 3  # 翻前3页，扩大样本量
        
        for page in range(1, max_pages + 1):
            payload = {
                "page_index": page,
                "page_size": 300,  # 拉满，每页300个
                "show_recently_price": False,  # 不需要近期价格，减少数据量
                "filter": filter_payload
            }

            try:
                # 只在启动时绑定一次IP，之后不再频繁绑定（因为IP没变）
                # 如果这是第一次运行（last_bind_time == 0），才绑定
                if self.last_bind_time == 0:
                    self.logger.debug("首次运行，绑定IP...")
                    self.bind_local_ip(force=True)
                
                time.sleep(1)  # 遵守频率限制
                
                resp = self.session.post(url, json=payload, timeout=15, verify=False)
                
                if resp.status_code == 401:
                    self.logger.warning(f"获取排行榜第{page}页失败: HTTP 401，尝试重新绑定IP...")
                    # 强制绑定（忽略冷却时间），因为401说明IP可能失效了
                    if self.bind_local_ip(force=True):
                        time.sleep(2)  # 等待绑定生效
                        # 重试一次
                        resp = self.session.post(url, json=payload, timeout=15, verify=False)
                    else:
                        # 如果强制绑定失败（可能是冷却中），等待冷却时间后再试
                        now = time.time()
                        if self.last_bind_time > 0:
                            wait_time = max(0, 35 - (now - self.last_bind_time))
                            if wait_time > 0:
                                self.logger.debug(f"等待IP绑定冷却时间: {wait_time:.1f}秒...")
                                time.sleep(wait_time)
                                if self.bind_local_ip(force=True):
                                    time.sleep(2)
                                    resp = self.session.post(url, json=payload, timeout=15, verify=False)
                                else:
                                    self.logger.error("重新绑定IP失败，停止获取排行榜")
                                    break
                        else:
                            self.logger.error("重新绑定IP失败，停止获取排行榜")
                            break
                
                if resp.status_code != 200:
                    self.logger.warning(f"获取排行榜第{page}页失败: HTTP {resp.status_code}")
                    break
                
                result = resp.json()
                code = result.get("code")
                
                if code not in [200, 201]:
                    msg = result.get("msg", "未知错误")
                    self.logger.warning(f"获取排行榜第{page}页失败: {msg} (code: {code})")
                    break
                
                data = result.get("data", {})
                items = data.get("data", [])
                
                if not items:
                    # 没有数据了，停止翻页
                    break
                
                all_items.extend(items)
                self.logger.debug(f"  第{page}页获取到 {len(items)} 个饰品")
                
            except Exception as e:
                self.logger.error(f"获取排行榜第{page}页异常: {e}")
                break
        
        return all_items

    def get_lease_num_from_chart(self, good_id: int, period: int = 7) -> Optional[int]:
        """
        从chart接口获取在租数量（备用方法）
        当get_item_details失败时使用
        
        :param good_id: 饰品ID
        :param period: 查询周期（7=近7天，30=近30天，90=近90天）
                      注意：返回的是该周期内最新的在租数量（数组最后一个值）
        :return: 在租数量（当前值）
        """
        url = f"{self.base_url}/info/chart"
        payload = {
            "good_id": good_id,
            "key": "lease_num",  # 查询在租数量
            "platform": 2,  # 悠悠有品平台
            "period": period,  # 查询周期（使用7天获取最新值，减少数据量）
            "style": "all_style"
        }
        
        try:
            time.sleep(0.5)  # 遵守频率限制
            resp = self.session.post(url, json=payload, timeout=10, verify=False)
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get("code") == 200:
                    data = result.get("data", {})
                    main_data = data.get("main_data", [])
                    if main_data and len(main_data) > 0:
                        # 返回最新的在租数量（最后一个值）
                        return int(main_data[-1]) if main_data[-1] is not None else None
            return None
        except Exception as e:
            self.logger.debug(f"从chart接口获取在租数量失败: {e}")
            return None

    def get_item_details(self, good_id: int) -> Optional[dict]:
        """
        获取详情：查在租数量、日租金、在售数量
        这是"验资"的关键步骤，用于识别"僵尸盘"
        优化：避免频繁触发429，采用渐进式重试策略
        """
        url = f"{self.base_url}/info/good"  # 注意：API路径是 /info/good，不是 /info/get_good
        
        # GET 请求的 headers（不需要 Content-Type: application/json）
        # 只保留 ApiToken 和 User-Agent，与 chart 接口保持一致
        get_headers = {
            "ApiToken": self.api_token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # 优化重试策略：增加重试次数，拉长间隔，避免频繁触发429
        for retry in range(5):
            try:
                # 基础间隔拉长，避免请求过快
                if retry > 0:
                    sleep_time = 1.0 + retry * 0.5  # 第1次重试1.5秒，第2次2秒，以此类推
                    time.sleep(sleep_time)
                else:
                    time.sleep(0.5)  # 首次请求前短暂等待
                
                # GET 请求：使用 id 作为参数名，显式传递 headers
                # 注意：必须显式传递 headers，因为 session 的默认 headers 包含 Content-Type，GET 请求不需要
                params = {"id": good_id}
                resp = self.session.get(url, params=params, headers=get_headers, timeout=10, verify=False)
                
                # 调试：记录请求详情（仅在第一次重试时）
                if retry == 0:
                    self.logger.debug(f"详情接口请求: URL={url}?id={good_id}, status={resp.status_code}")
                    if resp.status_code == 401:
                        # 尝试获取响应内容，看看是否有错误信息
                        try:
                            error_data = resp.json()
                            error_msg = error_data.get("msg", "")
                            error_code = error_data.get("code", "")
                            self.logger.debug(f"401响应详情: code={error_code}, msg={error_msg}")
                        except:
                            self.logger.debug(f"401响应文本: {resp.text[:200]}")
                
                # 处理 429 限流：等待更长时间
                if resp.status_code == 429:
                    self.logger.warning(f"详情接口 429 限流 (重试 {retry+1}/5)，等待 10 秒...")
                    time.sleep(10)  # 遇到限流，睡久一点
                    continue
                
                # 处理 401 未授权：既然IP没变，就不应该频繁绑定
                if resp.status_code == 401:
                    self.logger.debug(f"详情接口 401 未授权 (重试 {retry+1}/5)")
                    # 只在第一次遇到401时尝试绑定一次（如果距离上次绑定超过60秒）
                    # 之后不再绑定，因为IP没变，绑定也没用
                    if retry == 0:
                        now = time.time()
                        # 如果距离上次绑定超过60秒，可能是API Token的问题，尝试重新绑定一次
                        if self.last_bind_time == 0 or (now - self.last_bind_time) > 60:
                            self.logger.info("首次401错误，尝试重新绑定IP（仅一次）...")
                            if self.bind_local_ip(force=True):
                                time.sleep(3)
                        else:
                            self.logger.debug(f"距离上次绑定仅 {now - self.last_bind_time:.1f}秒，IP未变化，跳过绑定")
                    # 其他重试只等待，不绑定
                    time.sleep(3 + retry * 1)  # 渐进式等待：3秒、4秒、5秒...
                    continue
                
                # 处理其他HTTP错误
                if resp.status_code != 200:
                    # 如果使用 id 失败，尝试 good_id（某些 API 版本可能不同）
                    if resp.status_code == 404 or resp.status_code == 400:
                        params = {"good_id": good_id}
                        resp = self.session.get(url, params=params, headers=get_headers, timeout=10, verify=False)
                        if resp.status_code != 200:
                            self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP {resp.status_code}")
                            if retry < 4:
                                continue
                            return None
                    else:
                        self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP {resp.status_code}")
                        if retry < 4:
                            time.sleep(2)
                            continue
                        return None
                
                # 解析响应
                result = resp.json()
                code = result.get("code")
                msg = result.get("msg", "")
                
                # 检查 API 返回码
                if code not in [200, 201]:
                    # 记录具体错误信息
                    if code == 429:
                        self.logger.warning(f"获取饰品 {good_id} 详情失败: 频率限制 (429)，等待 10 秒...")
                        time.sleep(10)
                        continue
                    elif code == 401:
                        self.logger.debug(f"获取饰品 {good_id} 详情失败: 未授权 (401)")
                        # 既然IP没变，就不应该频繁绑定
                        if retry == 0:
                            now = time.time()
                            if self.last_bind_time == 0 or (now - self.last_bind_time) > 60:
                                self.logger.info("首次401错误，尝试重新绑定IP（仅一次）...")
                                if self.bind_local_ip(force=True):
                                    time.sleep(3)
                            else:
                                self.logger.debug(f"距离上次绑定仅 {now - self.last_bind_time:.1f}秒，IP未变化，跳过绑定")
                        time.sleep(3 + retry * 1)
                        continue
                    else:
                        self.logger.debug(f"获取饰品 {good_id} 详情失败: code={code}, msg={msg}")
                        if retry < 4:
                            time.sleep(2)
                            continue
                        return None
                
                # 成功获取数据
                data = result.get("data", {})
                # 根据实际 API 响应结构调整
                goods_info = data.get("goods_info") or data.get("data") or data
                
                # 检查是否真的获取到了数据
                if not goods_info or (isinstance(goods_info, dict) and not goods_info):
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 数据为空")
                    if retry < 4:
                        time.sleep(1)
                        continue
                    return None
                
                return goods_info
                
            except requests.exceptions.Timeout:
                if retry < 4:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 请求超时，重试 {retry+1}/5...")
                    time.sleep(1 + retry * 0.5)
                    continue
                else:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 请求超时（已重试5次）")
                    return None
            except requests.exceptions.RequestException as e:
                if retry < 4:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 网络错误 - {e}，重试 {retry+1}/5...")
                    time.sleep(1 + retry * 0.5)
                    continue
                else:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 网络错误 - {e}（已重试5次）")
                    return None
            except Exception as e:
                self.logger.debug(f"获取饰品 {good_id} 详情失败: {type(e).__name__} - {e}")
                if retry < 4:
                    time.sleep(1)
                    continue
                return None
        
        return None

    def get_lease_stability(self, good_id: int, period: int = 90) -> Optional[float]:
        """
        稳定性检查：计算租金波动率
        返回: 波动率 (0.0 - 1.0). 越低越好
        
        :param good_id: 饰品ID
        :param period: 查询周期（30=近30天，90=近90天）
                      默认90天，与价格涨跌分析保持一致
        :return: 波动率（变异系数 = 标准差/均值），如果失败返回 None
        """
        url = f"{self.base_url}/info/chart"  # 注意：API路径是 /info/chart，不是 /info/get_chart
        payload = {
            "good_id": good_id,
            "key": "short_lease_price",  # 检查短租价格走势
            "platform": 2,  # 悠悠有品平台
            "period": period,  # 近90天（与价格涨跌分析保持一致）
            "style": "all_style"
        }

        # 优化重试策略：增加重试次数，拉长间隔，避免频繁触发429
        for retry in range(3):
            try:
                # 基础间隔拉长
                if retry > 0:
                    sleep_time = 1.0 + retry * 0.5
                    time.sleep(sleep_time)
                else:
                    time.sleep(0.5)
                
                resp = self.session.post(url, json=payload, timeout=10, verify=False)
                
                # 处理 429 限流
                if resp.status_code == 429:
                    self.logger.warning(f"租金稳定性接口 429 限流 (重试 {retry+1}/3)，等待 10 秒...")
                    time.sleep(10)
                    continue
                
                # 处理 401 未授权：既然IP没变，就不应该频繁绑定
                if resp.status_code == 401:
                    self.logger.debug(f"租金稳定性接口 401 未授权 (重试 {retry+1}/3)")
                    # 只在第一次遇到401时尝试绑定一次（如果距离上次绑定超过60秒）
                    if retry == 0:
                        now = time.time()
                        if self.last_bind_time == 0 or (now - self.last_bind_time) > 60:
                            self.logger.info("首次401错误，尝试重新绑定IP（仅一次）...")
                            if self.bind_local_ip(force=True):
                                time.sleep(3)
                        else:
                            self.logger.debug(f"距离上次绑定仅 {now - self.last_bind_time:.1f}秒，IP未变化，跳过绑定")
                    time.sleep(3 + retry * 1)
                    continue
                
                if resp.status_code != 200:
                    self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: HTTP {resp.status_code}")
                    if retry < 2:
                        time.sleep(2)
                        continue
                    return None
                
                result = resp.json()
                code = result.get("code")
                
                if code not in [200, 201]:
                    if code == 429:
                        self.logger.warning(f"获取饰品 {good_id} 租金稳定性失败: 频率限制 (429)，等待 10 秒...")
                        time.sleep(10)
                        continue
                    elif code == 401:
                        self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: 未授权 (401)")
                        # 既然IP没变，就不应该频繁绑定
                        if retry == 0:
                            now = time.time()
                            if self.last_bind_time == 0 or (now - self.last_bind_time) > 60:
                                self.logger.info("首次401错误，尝试重新绑定IP（仅一次）...")
                                if self.bind_local_ip(force=True):
                                    time.sleep(3)
                            else:
                                self.logger.debug(f"距离上次绑定仅 {now - self.last_bind_time:.1f}秒，IP未变化，跳过绑定")
                        time.sleep(3 + retry * 1)
                        continue
                    else:
                        self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: code={code}")
                        if retry < 2:
                            time.sleep(2)
                            continue
                        return None
                
                data = result.get('data', {})
                prices = data.get('main_data', [])
                
                # 数据清洗，去除None
                if prices:
                    prices = [p for p in prices if p is not None]
                
                if not prices or len(prices) < 5:
                    self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: 数据不足（少于5个数据点）")
                    return None
                
                # 计算变异系数 (标准差/均值)
                prices_float = [float(p) for p in prices if p]
                if not prices_float:
                    self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: 数据为空")
                    return None
                
                avg = sum(prices_float) / len(prices_float)
                if avg == 0:
                    return 0.0
                
                # 计算标准差
                std = (sum((x - avg) ** 2 for x in prices_float) / len(prices_float)) ** 0.5
                
                # 变异系数 = 标准差 / 均值
                volatility = std / avg
                return volatility
                
            except requests.exceptions.Timeout:
                if retry < 1:
                    time.sleep(1)
                    continue
                else:
                    self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: 请求超时（已重试2次）")
                    return None
            except Exception as e:
                self.logger.debug(f"获取饰品 {good_id} 租金稳定性失败: {type(e).__name__} - {e}")
                return None
        
        return None

    def run_scan(self) -> List[dict]:
        """
        执行扫描流程
        :return: 白名单列表
        """
        self.logger.info("=" * 60)
        self.logger.info(f"🚀 [Scanner] 启动严选模式 (价格>{self.MIN_PRICE}元 | 在租>{self.MIN_LEASE_COUNT}人 | 日租>{self.MIN_DAILY_RENT}元)")
        self.logger.info("=" * 60)

        # 从配置读取参数
        invest_config = self.config.get("uu_auto_invest", {})
        scanner_config = self.config.get("scanner", {})
        
        # --- 策略 A: 稳健型 (严选模式) ---
        # 根据 API 文档，可以直接使用 filter 参数过滤，减少后续 API 调用
        filter_steady = {
            "排序": ["租赁_短租收益率(年化)"],  # 必填字段，按年化收益率排序
            "类型": scanner_config.get("filter_types_steady", ["不限_步枪", "不限_手枪", "不限_微型冲锋枪", "不限_探员"]),
            "价格最低价": self.MIN_PRICE,  # 价格硬门槛：200元
            "价格最高价": scanner_config.get("max_price_steady", 3000),
            "短租收益最低": scanner_config.get("min_roi_steady", 20),  # 年化20%以上
            "在售最少": scanner_config.get("min_on_sale_steady", 50),  # 确保流动性
            "出租最少": self.MIN_LEASE_COUNT  # 在租数量硬门槛：30人（API 层面过滤，避免调用详情接口）
        }
        
        # --- 策略 B: 重资产型 (匕首/手套) ---
        filter_heavy = {
            "排序": ["租赁_短租收益率(年化)"],  # 必填字段，按年化收益率排序
            "类型": scanner_config.get("filter_types_aggressive", ["不限_匕首", "不限_手套"]),
            "价格最低价": self.MIN_PRICE,  # 价格硬门槛：200元
            "价格最高价": scanner_config.get("max_price_aggressive", 8000),
            "短租收益最低": scanner_config.get("min_roi_aggressive", 30),  # 年化30%以上
            "在售最少": scanner_config.get("min_on_sale_aggressive", 20),
            "出租最少": self.MIN_LEASE_COUNT  # 在租数量硬门槛：30人（API 层面过滤，避免调用详情接口）
        }

        # 第一步：利用 API 强大的 Filter 功能进行海选（双轨制）
        self.logger.info("📡 策略A: 正在获取稳健型饰品（步枪/探员/微冲/手枪）...")
        list_steady = self.get_rank_list(filter_steady)
        self.logger.info(f"  获取到 {len(list_steady)} 个稳健型候选")
        
        time.sleep(1)  # 避免请求过快
        
        self.logger.info("📡 策略B: 正在获取重资产型饰品（匕首/手套）...")
        list_heavy = self.get_rank_list(filter_heavy)
        self.logger.info(f"  获取到 {len(list_heavy)} 个重资产型候选")
        
        raw_list = list_steady + list_heavy
        
        # 去重
        seen = set()
        unique_list = []
        for item in raw_list:
            item_id = item.get('id') or item.get('good_id')
            if item_id and item_id not in seen:
                unique_list.append(item)
                seen.add(item_id)
        
        if not unique_list:
            self.logger.error("无法获取排行榜数据，选品终止")
            return []

        self.logger.info(f"📡 API共拉取到 {len(unique_list)} 个初始目标（已去重），开始智能分析...")

        final_whitelist = []

        # 第二步：本地金融逻辑精选（严选模式 - 流动性硬指标）
        total_items = len(unique_list)
        consecutive_401_errors = 0  # 连续 401 错误计数
        max_401_errors = 5  # 最多允许 5 个连续 401 错误
        
        for index, item in enumerate(unique_list):
            name = item.get("name", "未知")
            good_id = item.get("id") or item.get("good_id")
            
            if not good_id:
                continue

            self.logger.info(f"[{index+1}/{total_items}] 分析: {name}")
            
            # 记录基础数据
            yyyp_sell_price = float(item.get('yyyp_sell_price', 0) or 0)
            yyyp_lease_annual = float(item.get('yyyp_lease_annual', 0) or 0)
            self.logger.info(f"  📊 基础数据: 价格={yyyp_sell_price:.2f}元 | 年化={yyyp_lease_annual:.1f}%")

            # 基础过滤：90天跌幅（不能跌太狠）
            # 注意：这是API计算的90天价格涨跌幅
            rate_90 = float(item.get('sell_price_rate_90', 0) or 0)
            if rate_90 < -15:  # 跌太狠的不要
                self.logger.info(f"  ❌ [淘汰] {name}: 90天价格跌幅过大 ({rate_90:.1f}% < -15%)")
                time.sleep(0.3)
                continue
            else:
                self.logger.debug(f"  ✓ 90天价格跌幅检查通过: {rate_90:.1f}% (时间范围: 90天)")

            # === 核心过滤：从排行榜数据中获取关键指标 ===
            # 根据 API 文档，get_rank_list 已返回 yyyp_sell_num 和 yyyp_lease_price
            # 尝试从排行榜数据中直接获取在租数量（如果 API 返回了该字段）
            
            # 1. 先从排行榜数据获取所有可用字段
            sell_num = int(item.get('yyyp_sell_num', 0) or 0)  # 在售数量
            daily_rent = float(item.get('yyyp_lease_price', 0) or 0)  # 日租金
            
            # 尝试从排行榜数据中获取在租数量（如果 API 返回了该字段）
            # 注意：根据 API 文档，排行榜数据可能不包含在租数量，但我们可以尝试获取
            lease_num_from_rank = item.get('yyyp_lease_num')  # 可能为 None
            
            # 2. 先进行基础检查（不需要在租数量）
            # 3. "甚至不够电费"熔断（拒绝"几毛钱"生意）
            if daily_rent < self.MIN_DAILY_RENT:
                self.logger.info(f"  ❌ [淘汰] {name}: 日租金过低 ({daily_rent:.2f}元 < {self.MIN_DAILY_RENT}元)")
                time.sleep(0.3)
                continue
            else:
                self.logger.debug(f"  ✓ 日租金检查通过: {daily_rent:.2f}元")
            
            # 4. 获取在租数量（优先使用排行榜数据，如果不存在则调用详情接口）
            lease_num = 0
            details = None
            
            # 如果排行榜数据中已有在租数量，直接使用
            if lease_num_from_rank is not None:
                lease_num = int(lease_num_from_rank)
                self.logger.info(f"  ✓ 从排行榜数据获取在租数量: {lease_num}人")
            else:
                # 如果排行榜数据中没有，尝试调用详情接口
                self.logger.debug(f"  📡 排行榜数据中无在租数量，尝试调用详情接口...")
                
                # 在调用详情接口之前，确保IP已绑定（如果距离上次绑定超过30秒，重新绑定）
                now = time.time()
                if self.last_bind_time == 0 or (now - self.last_bind_time) > 30:
                    self.logger.debug(f"距离上次绑定已超过30秒，重新绑定IP以确保详情接口可用...")
                    self.bind_local_ip(force=True)
                    time.sleep(1)  # 等待绑定生效
                
                details = self.get_item_details(good_id)
                
                if details:
                    # 成功获取详情
                    consecutive_401_errors = 0  # 重置错误计数
                    lease_num = int(details.get('yyyp_lease_num', 0) or 0)
                    sell_num = int(details.get('yyyp_sell_num', 0) or sell_num)
                    daily_rent = float(details.get('yyyp_lease_price', 0) or daily_rent)
                    self.logger.info(f"  ✓ 从详情接口获取数据: 在租={lease_num}人 | 在售={sell_num}人 | 日租={daily_rent:.2f}元")
                else:
                    # 详情接口失败，尝试使用chart接口作为备用
                    self.logger.debug(f"  📡 详情接口失败，尝试使用chart接口获取在租数量...")
                    lease_num_from_chart = self.get_lease_num_from_chart(good_id)
                    
                    if lease_num_from_chart is not None:
                        lease_num = lease_num_from_chart
                        consecutive_401_errors = 0
                        self.logger.info(f"  ✓ 从chart接口获取在租数量: {lease_num}人 (当前值, 来自7天数据的最新值)")
                    else:
                        # 所有方法都失败
                        self.logger.warning(f"  ❌ [淘汰] {name}: 无法获取在租数量（详情接口和chart接口均失败），宁缺毋滥 -> 跳过")
                        consecutive_401_errors += 1
                        time.sleep(0.5)
                        continue

            # 2. "僵尸盘"熔断（核心诉求：拒绝"2人租"惨案）
            # 注意：由于 filter 已经过滤了，这个检查主要是双重验证
            if lease_num < self.MIN_LEASE_COUNT:
                self.logger.info(f"  ❌ [淘汰] {name}: 在租人数不足 ({lease_num}人 < {self.MIN_LEASE_COUNT}人)")
                time.sleep(0.3)
                continue
            else:
                self.logger.debug(f"  ✓ 在租人数检查通过: {lease_num}人")

            # 3. "甚至不够电费"熔断（拒绝"几毛钱"生意）
            # 注意：这个检查已经在上面进行了，这里可以删除（但保留作为双重验证）
            # 实际上，由于 filter 已经过滤了日租金，这个检查主要是双重验证

            # 4. "供过于求"熔断（出租率计算）
            # 如果卖的人有500个，租的人只有30个，出租率 6%，很难轮到你
            # 注意：在租数量和在售数量都是当前值（实时数据），时间范围一致
            if sell_num > 0:
                lease_ratio = lease_num / sell_num
            else:
                lease_ratio = 0
            
            if lease_ratio < self.MIN_LEASE_RATIO:
                self.logger.info(f"  ❌ [淘汰] {name}: 出租率过低 ({lease_ratio:.1%} < {self.MIN_LEASE_RATIO:.1%}) | 在售:{sell_num}人 在租:{lease_num}人 (时间范围: 当前值)")
                time.sleep(0.3)
                continue
            else:
                self.logger.debug(f"  ✓ 出租率检查通过: {lease_ratio:.1%} (在售:{sell_num}人 在租:{lease_num}人, 时间范围: 当前值)")

            # 5. 租金稳定性检查
            # 注意：使用90天数据计算波动率，与价格涨跌分析保持一致
            self.logger.debug(f"  📡 正在检查租金稳定性（90天数据）...")
            volatility = self.get_lease_stability(good_id, period=90)
            if volatility is None:
                # 如果无法获取波动率数据，记录警告但不跳过（因为可能是API问题，不是饰品问题）
                self.logger.warning(f"  ⚠️ {name}: 无法获取租金稳定性数据，跳过波动率检查（可能是API限流或401错误）")
                volatility = 0.0  # 设置为0，表示无法判断
            elif volatility > self.MAX_VOLATILITY:
                self.logger.info(f"  ❌ [淘汰] {name}: 租金波动率过高 ({volatility:.1%} > {self.MAX_VOLATILITY:.1%}, 时间范围: 90天)")
                time.sleep(0.3)
                continue
            else:
                self.logger.debug(f"  ✓ 租金稳定性检查通过: {volatility:.1%} (时间范围: 90天)")

            # === 通过所有测试 ===
            yyyp_lease_annual = item.get("yyyp_lease_annual", 0)
            roi = float(yyyp_lease_annual) / 100.0
            yyyp_sell_price = float(item.get('yyyp_sell_price', 0))
            buff_sell_price = float(item.get('buff_sell_price', 0))
            buy_limit = round(yyyp_sell_price * 0.92, 2)  # 建议92折求购
            
            # 判断资产类型
            is_heavy = any(x in name for x in ["★", "手套", "匕首", "刀", "蝴蝶", "爪子", "M9", "刺刀"])
            asset_type = "重资产" if is_heavy else "稳健型"

            self.logger.info(f"  ✅ [入选] {name}")
            self.logger.info(f"     📊 完整数据:")
            self.logger.info(f"        - 价格: {yyyp_sell_price:.2f}元")
            self.logger.info(f"        - 日租: {daily_rent:.2f}元")
            self.logger.info(f"        - 在租: {lease_num}人")
            self.logger.info(f"        - 在售: {sell_num}人")
            self.logger.info(f"        - 出租率: {lease_ratio:.1%} (当前值)")
            self.logger.info(f"        - 年化: {yyyp_lease_annual:.1f}% (当前值)")
            self.logger.info(f"        - 90天价格涨跌: {rate_90:.1f}% (90天)")
            self.logger.info(f"        - 租金波动率: {volatility:.1%} (90天, 变异系数)")
            self.logger.info(f"        - 推荐求购价: {buy_limit:.2f}元")

            final_whitelist.append({
                "templateId": str(good_id),
                "name": name,
                "roi": roi,
                "roi_percent": yyyp_lease_annual,
                "buy_limit": buy_limit,
                "current_price": yyyp_sell_price,
                "yyyp_sell_price": yyyp_sell_price,
                "buff_sell_price": buff_sell_price,
                "daily_rent": daily_rent,
                "lease_num": lease_num,
                "sell_num": sell_num,
                "lease_ratio": round(lease_ratio, 4),
                "lease_volatility": round(volatility, 4),
                "sell_price_rate_90": rate_90,
                "asset_type": asset_type,
                "selected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            # 避免请求过快
            time.sleep(0.5)

        self.logger.info("=" * 60)
        if final_whitelist:
            self.logger.info(f"🎉 筛选结束! 最终入库 {len(final_whitelist)} 个硬通货。")
        else:
            self.logger.warning("⚠️ 筛选结束，没有找到符合'严选标准'的饰品，建议稍作休息或微调参数。")
        self.logger.info("=" * 60)

        return final_whitelist

    def save_whitelist(self, whitelist: List[dict]):
        """
        保存白名单到文件（简化格式）
        :param whitelist: 白名单列表
        """
        try:
            os.makedirs(os.path.dirname(self.whitelist_path), exist_ok=True)
            with open(self.whitelist_path, "w", encoding="utf-8") as f:
                json.dump(whitelist, f, ensure_ascii=False, indent=2)
            
            self.logger.info(f"白名单已保存到: {self.whitelist_path}")
            self.logger.info(f"共 {len(whitelist)} 个优质饰品已入库")
        except Exception as e:
            self.logger.error(f"保存白名单失败: {e}")

    def run(self):
        """执行完整的扫描流程"""
        if not self.api_token:
            self.logger.error("未配置 API Token，无法运行")
            return

        try:
            # 第一步：自动绑定本机IP（解决单IP白名单限制）
            self.logger.info("=" * 60)
            self.logger.info("步骤1: 绑定本机IP到API白名单")
            self.logger.info("=" * 60)
            bind_success = self.bind_local_ip()
            if not bind_success:
                self.logger.warning("IP绑定失败，但继续尝试运行（可能IP已在白名单中）")
            time.sleep(1)  # 等待1秒，确保绑定生效
            
            # 第二步：执行扫描
            self.logger.info("")
            whitelist = self.run_scan()
            
            if whitelist:
                # 保存白名单
                self.save_whitelist(whitelist)
                
                # 打印摘要
                self.logger.info("\n" + "=" * 60)
                self.logger.info("选品摘要")
                self.logger.info("=" * 60)
                for i, item in enumerate(whitelist, 1):
                    self.logger.info(f"{i}. {item['name']}")
                    asset_type = item.get('asset_type', '未知')
                    roi_percent = item.get('roi_percent', 0)
                    daily_rent = item.get('daily_rent', 0)
                    lease_num = item.get('lease_num', 0)
                    lease_ratio = item.get('lease_ratio', 0) * 100
                    buy_limit = item.get('buy_limit', 0)
                    self.logger.info(f"   类型: {asset_type} | ROI: {roi_percent:.1f}% | "
                                   f"日租: {daily_rent:.2f}元 | 在租: {lease_num}人 | "
                                   f"出租率: {lease_ratio:.1f}% | 推荐求购价: {buy_limit:.2f}元")
                self.logger.info("=" * 60)
            else:
                self.logger.warning("未找到符合条件的饰品，请调整筛选参数")
                
        except Exception as e:
            handle_caught_exception(e, "Scanner")
            self.logger.error("扫描过程出现异常")


class ScannerPlugin:
    """
    Scanner 插件包装器
    用于在主程序中自动运行 Scanner
    """
    
    def __init__(self, steam_client, steam_client_mutex, config):
        self.logger = PluginLogger("ScannerPlugin")
        self.config = config
        self.steam_client = steam_client
        self.steam_client_mutex = steam_client_mutex
        self.scanner = None

    def init(self) -> bool:
        """初始化插件"""
        scanner_config = self.config.get("scanner", {})
        if not scanner_config.get("enable", False):
            return False

        try:
            self.scanner = CSQAQScanner()
            self.logger.info("Scanner 插件初始化成功")
            return False  # 返回 False 表示初始化成功
        except Exception as e:
            handle_caught_exception(e, "ScannerPlugin")
            self.logger.error("Scanner 插件初始化失败")
            return True  # 返回 True 表示初始化失败

    def exec(self):
        """执行函数 - 启动时自动运行一次"""
        scanner_config = self.config.get("scanner", {})
        if not scanner_config.get("enable", False):
            return

        # 启动时立即执行一次
        if scanner_config.get("run_on_start", True):
            self.logger.info("=" * 60)
            self.logger.info("Scanner 插件启动，开始执行选品扫描...")
            self.logger.info("=" * 60)
            try:
                self.scanner.run()
                self.logger.info("Scanner 插件执行完成")
            except Exception as e:
                handle_caught_exception(e, "ScannerPlugin")
                self.logger.error("Scanner 插件执行失败")
        else:
            self.logger.info("Scanner 插件已启用，但 run_on_start 为 false，跳过启动时执行")
        
        # Scanner 插件执行完成后直接返回，不进入循环
        # 因为选品扫描是一次性任务，不需要持续运行


def main():
    """主函数 - 独立运行（用于单体测试）"""
    print("=" * 60)
    print("Scanner 模块单体测试")
    print("=" * 60)
    print("提示：确保 config.json5 中已配置 csqaq_api_token")
    print("=" * 60)
    print()
    
    try:
        scanner = CSQAQScanner()
        if not scanner.api_token:
            print("❌ 错误：未配置 csqaq_api_token")
            print("请在 config.json5 的 uu_auto_invest 配置中添加：")
            print('  "csqaq_api_token": "你的TOKEN"')
            return
        
        print(f"✅ API Token 已配置（长度: {len(scanner.api_token)}）")
        print(f"✅ 价格硬门槛: {scanner.MIN_PRICE}元")
        print(f"✅ 日租金底线: {scanner.MIN_DAILY_RENT}元")
        print(f"✅ 最小在租人数: {scanner.MIN_LEASE_COUNT}人")
        print(f"✅ 最小出租率: {scanner.MIN_LEASE_RATIO*100:.0f}%")
        print()
        print("开始执行扫描...")
        print()
        
        scanner.run()
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断测试")
    except Exception as e:
        print(f"\n\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

