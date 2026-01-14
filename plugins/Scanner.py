"""
CSQAQ 智能选品扫描器 (Scanner)
三期过滤法：从高回报榜单中筛选出真正的理财枪皮
建议每天中午 12:00 或晚上 20:00 运行一次
"""

import json
import os
import time
from typing import Optional, List, Dict
from datetime import datetime

import json5
import requests

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
        
        # 选品硬指标配置
        self.MIN_ROI = invest_config.get("min_roi", 0.25)  # 最小年化回报 25%
        self.MAX_ROI = invest_config.get("max_roi", 0.55)  # 最大年化回报 55%（过高通常有诈）
        self.MIN_PRICE = invest_config.get("min_price", 100)  # 价格底线
        self.MAX_PRICE = invest_config.get("max_price", 2000)  # 价格上限
        self.MIN_LEASE_NUM = invest_config.get("min_lease_num", 30)  # 必须有30人以上在租（保热度）
        self.MAX_VOLATILITY = invest_config.get("max_volatility", 0.15)  # 最大价格波动率 15%
        self.MIN_TREND_90D = invest_config.get("min_trend_90d", -10)  # 90天最小涨跌幅 -10%
        
        # API 配置
        self.api_token = self._get_api_token()
        self.base_url = "https://api.csqaq.com/api/v1"
        self.headers = {
            "ApiToken": self.api_token,
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
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
            
            resp = requests.post(url, headers=self.headers, timeout=15)
            
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

    def get_rank_list(self) -> List[dict]:
        """
        海选：获取短租收益榜前100名
        """
        url = f"{self.base_url}/info/get_rank_list"
        payload = {
            "page_index": 1,
            "page_size": 100,
            "filter": {
                "排序": ["租赁_短租收益率(年化)"],
                "价格最低价": self.MIN_PRICE,
                "价格最高价": self.MAX_PRICE
            }
        }

        try:
            self.logger.info("正在获取短租收益榜前100名...")
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
            self.logger.info(f"获取到 {len(items)} 个候选饰品")
            return items
            
        except Exception as e:
            self.logger.error(f"获取排行榜失败: {e}")
            return []

    def get_item_details(self, good_id: int) -> Optional[dict]:
        """
        精选：获取在租数量等热度指标
        """
        # 根据 CSQAQ API 文档，获取饰品详情使用 /info/get_good
        url = f"{self.base_url}/info/get_good"
        
        try:
            time.sleep(0.5)  # 遵守频率限制
            
            resp = requests.get(url, params={"good_id": good_id}, headers=self.headers, timeout=15)
            
            if resp.status_code != 200:
                return None
            
            result = resp.json()
            code = result.get("code")
            
            if code not in [200, 201]:
                return None
            
            data = result.get("data", {})
            # 根据实际 API 响应结构调整
            goods_info = data.get("goods_info") or data.get("data") or data
            return goods_info
            
        except Exception as e:
            self.logger.debug(f"获取饰品 {good_id} 详情失败: {e}")
            return None

    def get_stability_score(self, good_id: int) -> float:
        """
        终审：检查90天价格波动率
        返回波动率（0-1之间，越小越稳定）
        """
        # 根据 CSQAQ API 文档，获取图表数据使用 /info/get_chart
        url = f"{self.base_url}/info/get_chart"
        payload = {
            "good_id": good_id,
            "key": "sell_price",
            "platform": 2,  # 悠悠有品平台
            "period": 90,
            "style": "all_style"
        }

        try:
            time.sleep(0.5)  # 遵守频率限制
            
            resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
            
            if resp.status_code != 200:
                return 1.0  # 返回最大值表示不稳定
            
            result = resp.json()
            code = result.get("code")
            
            if code not in [200, 201]:
                return 1.0
            
            data = result.get("data", {})
            # 根据实际 API 响应结构调整
            chart_data = data.get("chart_data") or data
            prices = chart_data.get("main_data", [])
            
            if not prices or len(prices) < 20:
                return 1.0  # 数据不足，认为不稳定
            
            # 计算波动率: (最高-最低)/平均
            prices_float = [float(p) for p in prices if p]
            if not prices_float:
                return 1.0
            
            avg = sum(prices_float) / len(prices_float)
            if avg == 0:
                return 1.0
            
            volatility = (max(prices_float) - min(prices_float)) / avg
            return volatility
            
        except Exception as e:
            self.logger.debug(f"获取饰品 {good_id} 稳定性数据失败: {e}")
            return 1.0  # 出错时返回最大值表示不稳定

    def run_scan(self) -> List[dict]:
        """
        执行扫描流程
        :return: 白名单列表
        """
        self.logger.info("=" * 60)
        self.logger.info("🔍 开始每日量化选品（三期过滤法）")
        self.logger.info("=" * 60)

        # 第一步：海选
        raw_list = self.get_rank_list()
        if not raw_list:
            self.logger.error("无法获取排行榜数据，选品终止")
            return []

        final_whitelist = []
        total_items = len(raw_list)

        # 第二步：三期过滤
        for index, item in enumerate(raw_list):
            name = item.get("name", "未知")
            good_id = item.get("id") or item.get("good_id")
            
            if not good_id:
                continue

            self.logger.info(f"[{index+1}/{total_items}] 分析: {name}")

            # 1. 回报率初筛
            yyyp_lease_annual = item.get("yyyp_lease_annual", 0)
            if not yyyp_lease_annual:
                self.logger.debug(f"  - {name}: 缺少年化收益率数据，跳过")
                continue

            roi = float(yyyp_lease_annual) / 100.0
            if not (self.MIN_ROI <= roi <= self.MAX_ROI):
                self.logger.debug(f"  - {name}: ROI不达标 ({roi:.1%}，要求 {self.MIN_ROI:.1%}-{self.MAX_ROI:.1%})，跳过")
                continue

            # 2. 趋势初筛 (90天不跌超过10%)
            sell_price_rate_90 = float(item.get("sell_price_rate_90", 0))
            if sell_price_rate_90 < self.MIN_TREND_90D:
                self.logger.debug(f"  - {name}: 处于中长期下降通道 (90天跌幅 {sell_price_rate_90:.1f}%)，跳过")
                continue

            # 3. 详情深挖 (获取在租数量)
            details = self.get_item_details(good_id)
            if not details:
                self.logger.debug(f"  - {name}: 无法获取详情数据，跳过")
                continue

            yyyp_lease_num = int(details.get("yyyp_lease_num", 0) or item.get("yyyp_lease_num", 0))
            if yyyp_lease_num < self.MIN_LEASE_NUM:
                self.logger.debug(f"  - {name}: 在租热度不足 ({yyyp_lease_num} < {self.MIN_LEASE_NUM})，跳过")
                continue

            # 4. 稳定性终审 (90天价格波动低于15%)
            volatility = self.get_stability_score(good_id)
            if volatility > self.MAX_VOLATILITY:
                self.logger.debug(f"  - {name}: 价格波动过大 ({volatility:.1%} > {self.MAX_VOLATILITY:.1%})，跳过")
                continue

            # 所有检查通过，加入白名单
            yyyp_sell_price = float(item.get("yyyp_sell_price", 0))
            buy_limit = round(yyyp_sell_price * 0.92, 2)  # 求购建议价（市场价的92%）

            final_whitelist.append({
                "templateId": str(good_id),
                "name": name,
                "roi": roi,
                "roi_percent": yyyp_lease_annual,
                "buy_limit": buy_limit,
                "yyyp_sell_price": yyyp_sell_price,
                "volatility": round(volatility, 4),
                "yyyp_lease_num": yyyp_lease_num,
                "selected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            self.logger.info(f"  ✅ 选入白名单: {name} | 年化: {roi:.1%} | 波动: {volatility:.1%} | 推荐求购价: {buy_limit:.2f}元")

            # 避免请求过快
            if (index + 1) % 10 == 0:
                self.logger.info(f"已分析 {index+1}/{total_items} 个饰品，当前合格: {len(final_whitelist)} 个")
                time.sleep(2)  # 每10个休息2秒

        self.logger.info("=" * 60)
        self.logger.info(f"✨ 选品完成，共筛选出 {len(final_whitelist)} 款优质理财饰品")
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
                    self.logger.info(f"   ROI: {item['roi_percent']:.1f}% | "
                                   f"波动率: {item['volatility']*100:.1f}% | "
                                   f"推荐求购价: {item['buy_limit']:.2f}元")
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
    """主函数 - 独立运行"""
    scanner = CSQAQScanner()
    scanner.run()


if __name__ == "__main__":
    main()

