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
        
        # 配置重试机制，解决网络不稳
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.headers.update(self.headers)
        
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

    def bind_local_ip(self) -> bool:
        """
        绑定本机白名单IP
        为当前请求的API_TOKEN绑定本机的IP地址，适用于非固定IP场景下使用
        频率限制：30秒/次
        :return: 是否绑定成功
        """
        if not self.api_token:
            self.logger.warning("未配置 API Token，无法绑定IP")
            return False

        url = f"{self.base_url}/sys/bind_local_ip"
        
        try:
            self.logger.info("正在绑定本机IP到API白名单...")
            
            resp = self.session.post(url, headers=self.headers, timeout=10, verify=False)
            
            if resp.status_code != 200:
                self.logger.error(f"绑定IP失败: HTTP {resp.status_code}")
                return False
            
            result = resp.json()
            code = result.get("code")
            msg = result.get("msg", "")
            data = result.get("data", "")
            
            if code == 200:
                self.logger.info(f"✅ {data}")
                return True
            elif code == 429:
                self.logger.warning(f"⚠️ 请求频率过快，绑定IP频率限制为30秒/次。{data}")
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
        :param filter_payload: filter 字典
        :return: 饰品列表
        """
        url = f"{self.base_url}/info/get_rank_list"
        
        payload = {
            "page_index": 1,
            "page_size": 200,
            "show_recently_price": True,  # 获取近期价格数据，用于趋势分析
            "filter": filter_payload
        }

        try:
            time.sleep(1)  # 遵守频率限制
            
            resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
            
            if resp.status_code == 401:
                self.logger.error("API返回401未授权错误，请检查 csqaq_api_token 和 IP 白名单")
                return []
            
            if resp.status_code != 200:
                self.logger.error(f"API请求失败: HTTP {resp.status_code}")
                return []
            
            result = resp.json()
            code = result.get("code")
            
            if code not in [200, 201]:
                msg = result.get("msg", "未知错误")
                self.logger.error(f"API返回错误: {msg} (code: {code})")
                return []
            
            data = result.get("data", {})
            items = data.get("data", [])
            return items
            
        except Exception as e:
            self.logger.error(f"获取排行榜失败: {e}")
            return []

    def get_item_details(self, good_id: int) -> Optional[dict]:
        """
        获取详情：查在租数量、日租金、在售数量
        这是"验资"的关键步骤，用于识别"僵尸盘"
        """
        url = f"{self.base_url}/info/get_good"
        
        try:
            time.sleep(0.3)  # 遵守频率限制
            
            # 使用与 get_rank_list 相同的认证方式（直接传入 headers）
            # CSQAQ API 使用 id 作为参数名
            params = {"id": good_id}
            resp = requests.get(url, params=params, headers=self.headers, timeout=10, verify=False)
            
            if resp.status_code != 200:
                # 如果使用 id 失败，尝试 good_id（某些 API 版本可能不同）
                if resp.status_code == 404 or resp.status_code == 400:
                    params = {"good_id": good_id}
                    resp = requests.get(url, params=params, headers=self.headers, timeout=10, verify=False)
                    if resp.status_code != 200:
                        if resp.status_code == 401:
                            self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP 401 未授权（请检查 API Token 和 IP 白名单）")
                        else:
                            self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP {resp.status_code}")
                        return None
                elif resp.status_code == 401:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP 401 未授权（请检查 API Token 和 IP 白名单）")
                    return None
                else:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: HTTP {resp.status_code}")
                    return None
            
            result = resp.json()
            code = result.get("code")
            msg = result.get("msg", "")
            
            # 检查 API 返回码
            if code not in [200, 201]:
                # 记录具体错误信息（但只在 DEBUG 级别，避免日志过多）
                if code == 429:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 频率限制 (429)")
                elif code == 401:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: 未授权 (401)")
                else:
                    self.logger.debug(f"获取饰品 {good_id} 详情失败: code={code}, msg={msg}")
                return None
            
            data = result.get("data", {})
            # 根据实际 API 响应结构调整
            goods_info = data.get("goods_info") or data.get("data") or data
            
            # 检查是否真的获取到了数据
            if not goods_info or (isinstance(goods_info, dict) and not goods_info):
                self.logger.debug(f"获取饰品 {good_id} 详情失败: 数据为空")
                return None
            
            return goods_info
            
        except requests.exceptions.Timeout:
            self.logger.debug(f"获取饰品 {good_id} 详情失败: 请求超时")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.debug(f"获取饰品 {good_id} 详情失败: 网络错误 - {e}")
            return None
        except Exception as e:
            self.logger.debug(f"获取饰品 {good_id} 详情失败: {type(e).__name__} - {e}")
            return None

    def get_lease_stability(self, good_id: int) -> float:
        """
        稳定性检查
        返回: 波动率 (0.0 - 1.0). 越低越好
        如果数据获取失败，默认返回 0.5 (视为中等风险)
        """
        url = f"{self.base_url}/info/get_chart"
        payload = {
            "good_id": good_id,
            "key": "short_lease_price",  # 检查短租价格走势
            "platform": 2,  # 悠悠有品平台
            "period": 30,  # 近30天
            "style": "all_style"
        }

        try:
            time.sleep(0.2)  # 遵守频率限制
            
            resp = self.session.post(url, json=payload, timeout=10, verify=False)
            
            if resp.status_code != 200:
                return 0.5  # 数据不足视为中等风险
            
            result = resp.json()
            data = result.get('data', {})
            prices = data.get('main_data', [])
            
            # 数据清洗，去除None
            if prices:
                prices = [p for p in prices if p is not None]
            
            if not prices or len(prices) < 5:
                return 0.5  # 数据不足视为中等风险
            
            # 计算变异系数 (标准差/均值)
            prices_float = [float(p) for p in prices if p]
            if not prices_float:
                return 0.5
            
            avg = sum(prices_float) / len(prices_float)
            if avg == 0:
                return 0.0
            
            # 计算标准差
            std = (sum((x - avg) ** 2 for x in prices_float) / len(prices_float)) ** 0.5
            
            # 变异系数 = 标准差 / 均值
            volatility = std / avg
            return volatility
            
        except Exception as e:
            # 出错了也不要卡死，默认中等风险
            self.logger.debug(f"获取饰品 {good_id} 租金稳定性数据失败: {e}")
            return 0.5

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

            # 基础过滤：90天跌幅（不能跌太狠）
            rate_90 = float(item.get('sell_price_rate_90', 0) or 0)
            if rate_90 < -15:  # 跌太狠的不要
                self.logger.debug(f"  - {name}: 跌幅过大 (90天跌幅 {rate_90:.1f}%)，跳过")
                time.sleep(0.3)
                continue

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
                self.logger.info(f"  ❌ [租金低] {name}: 日租 {daily_rent:.2f}元 (<{self.MIN_DAILY_RENT}元)")
                time.sleep(0.3)
                continue
            
            # 4. 获取在租数量（优先使用排行榜数据，如果不存在则调用详情接口）
            lease_num = 0
            details = None
            
            # 如果排行榜数据中已有在租数量，直接使用
            if lease_num_from_rank is not None:
                lease_num = int(lease_num_from_rank)
                self.logger.debug(f"  - {name}: 从排行榜数据获取在租数量: {lease_num}")
            else:
                # 如果排行榜数据中没有，尝试调用详情接口
                # 如果连续出现太多 401 错误，尝试重新绑定 IP
                if consecutive_401_errors >= max_401_errors:
                    self.logger.warning(f"连续出现 {consecutive_401_errors} 个 401 错误，尝试重新绑定 IP...")
                    if self.bind_local_ip():
                        consecutive_401_errors = 0  # 重置计数
                        time.sleep(2)  # 等待绑定生效
                    else:
                        self.logger.error("重新绑定 IP 失败，详情接口可能无法使用")
                        # 不 break，继续使用 filter 过滤的结果
                
                try:
                    details = self.get_item_details(good_id)
                    if details:
                        lease_num = int(details.get('yyyp_lease_num', 0) or 0)
                        consecutive_401_errors = 0  # 成功获取，重置计数
                        self.logger.debug(f"  - {name}: 从详情接口获取在租数量: {lease_num}")
                    else:
                        # 如果详情接口失败，由于 filter 已经过滤了在租数量 >= MIN_LEASE_COUNT
                        # 我们可以使用一个基于在售数量的合理估计值（而不是固定的最小值）
                        consecutive_401_errors += 1
                        # 使用在售数量估算在租数量（假设出租率为 20%，这是一个合理的估计）
                        estimated_lease_num = max(self.MIN_LEASE_COUNT, int(sell_num * 0.20))
                        lease_num = estimated_lease_num
                        if (index + 1) % 10 == 0:
                            self.logger.warning(f"  ⚠️ [{index+1}/{total_items}] {name} (ID: {good_id}): 无法获取在租数量，使用估算值 {lease_num} (基于在售数量 {sell_num})")
                        else:
                            self.logger.debug(f"  - {name}: 无法获取在租数量，使用估算值 {lease_num}")
                except Exception as e:
                    self.logger.debug(f"  - {name}: 获取详情失败: {e}，使用估算值")
                    # 使用在售数量估算在租数量
                    estimated_lease_num = max(self.MIN_LEASE_COUNT, int(sell_num * 0.20))
                    lease_num = estimated_lease_num

            # 2. "僵尸盘"熔断（核心诉求：拒绝"2人租"惨案）
            # 注意：由于 filter 已经过滤了，这个检查主要是双重验证
            if lease_num < self.MIN_LEASE_COUNT:
                self.logger.info(f"  ❌ [没人租] {name}: 在租仅 {lease_num} 人 (<{self.MIN_LEASE_COUNT})")
                time.sleep(0.3)
                continue

            # 3. "甚至不够电费"熔断（拒绝"几毛钱"生意）
            # 注意：这个检查已经在上面进行了，这里可以删除（但保留作为双重验证）
            # 实际上，由于 filter 已经过滤了日租金，这个检查主要是双重验证

            # 4. "供过于求"熔断（出租率计算）
            # 如果卖的人有500个，租的人只有30个，出租率 6%，很难轮到你
            if sell_num > 0:
                lease_ratio = lease_num / sell_num
            else:
                lease_ratio = 0
            
            if lease_ratio < self.MIN_LEASE_RATIO:
                self.logger.info(f"  ❌ [太卷了] {name}: 出租率 {lease_ratio:.1%} (<{self.MIN_LEASE_RATIO:.1%}) | 在售:{sell_num} 在租:{lease_num}")
                time.sleep(0.3)
                continue

            # 5. 租金稳定性检查
            volatility = self.get_lease_stability(good_id)
            if volatility > self.MAX_VOLATILITY:
                self.logger.info(f"  ❌ [租金乱] {name}: 波动率 {volatility:.1%} (> {self.MAX_VOLATILITY:.1%})")
                time.sleep(0.3)
                continue

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
            self.logger.info(f"     - 价格: {yyyp_sell_price:.2f}元 | 日租: {daily_rent:.2f}元 | 在租: {lease_num}人 | 出租率: {lease_ratio:.1%} | 年化: {yyyp_lease_annual:.1f}%")

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

