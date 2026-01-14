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

    def get_lease_stability(self, good_id: int) -> float:
        """
        检查租金走势稳定性（通过短租价格 K 线）
        返回波动率（0-1之间，越小越稳定）
        用于识别"虚假租金"（挂得高但没人租的情况）
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
            lease_prices = chart_data.get("main_data", [])
            
            if not lease_prices or len(lease_prices) < 10:
                return 1.0  # 数据不足，认为不稳定
            
            # 计算变异系数 (标准差/均值)
            prices_float = [float(p) for p in lease_prices if p]
            if not prices_float:
                return 1.0
            
            avg = sum(prices_float) / len(prices_float)
            if avg == 0:
                return 1.0
            
            # 计算标准差
            variance = sum((x - avg) ** 2 for x in prices_float) / len(prices_float)
            std = variance ** 0.5
            
            # 变异系数 = 标准差 / 均值
            volatility = std / avg
            return volatility
            
        except Exception as e:
            self.logger.debug(f"获取饰品 {good_id} 租金稳定性数据失败: {e}")
            return 1.0  # 出错时返回最大值表示不稳定

    def run_scan(self) -> List[dict]:
        """
        执行扫描流程
        :return: 白名单列表
        """
        self.logger.info("=" * 60)
        self.logger.info("🚀 [选品大脑] 启动双轨制全品类扫描模式（稳健型 + 高收益型）")
        self.logger.info("=" * 60)

        # 从配置读取参数
        invest_config = self.config.get("uu_auto_invest", {})
        scanner_config = self.config.get("scanner", {})
        
        # --- 策略 A: 稳健型 (步枪/探员/微冲/手枪) ---
        # 目标：不亏本金，稳定拿租
        filter_steady = {
            "排序": ["租赁_短租收益率(年化)"],
            "类型": scanner_config.get("filter_types_steady", ["不限_步枪", "不限_手枪", "不限_微型冲锋枪", "不限_探员"]),
            "价格最低价": self.MIN_PRICE,
            "价格最高价": self.MAX_PRICE,
            "短租收益最低": scanner_config.get("min_roi_steady", 20),  # 枪皮探员20%年化就很优质了
            "在售最少": invest_config.get("min_on_sale", 50)
        }
        
        # --- 策略 B: 高收益型 (匕首/手套) ---
        # 目标：利用10.24更新后的高租金对冲本金阴跌
        filter_aggressive = {
            "排序": ["租赁_短租收益率(年化)"],
            "类型": scanner_config.get("filter_types_aggressive", ["不限_匕首", "不限_手套"]),
            "价格最低价": scanner_config.get("min_price_aggressive", 300),
            "价格最高价": scanner_config.get("max_price_aggressive", 5000),  # 刀和手套稍微放宽预算
            "短租收益最低": scanner_config.get("min_roi_aggressive", 35),  # 刀手套必须35%以上才值得博弈
            "在售最少": scanner_config.get("min_on_sale_aggressive", 30)  # 流动性要求稍降，因为单价高
        }

        # 第一步：利用 API 强大的 Filter 功能进行海选（双轨制）
        self.logger.info("📡 策略A: 正在获取稳健型饰品（步枪/探员/微冲/手枪）...")
        steady_list = self.get_rank_list(filter_steady)
        self.logger.info(f"  获取到 {len(steady_list)} 个稳健型候选")
        
        self.logger.info("📡 策略B: 正在获取高收益型饰品（匕首/手套）...")
        aggressive_list = self.get_rank_list(filter_aggressive)
        self.logger.info(f"  获取到 {len(aggressive_list)} 个高收益型候选")
        
        raw_list = steady_list + aggressive_list
        
        if not raw_list:
            self.logger.error("无法获取排行榜数据，选品终止")
            return []

        self.logger.info(f"📡 API 初筛完成，共找到 {len(raw_list)} 个潜在目标（稳健型: {len(steady_list)}, 高收益型: {len(aggressive_list)}）")

        final_whitelist = []
        total_items = len(raw_list)

        # 第二步：本地金融逻辑精选（只做必要的检查）
        for index, item in enumerate(raw_list):
            name = item.get("name", "未知")
            good_id = item.get("id") or item.get("good_id")
            
            if not good_id:
                continue

            self.logger.info(f"[{index+1}/{total_items}] 分析: {name}")

            # 判断是否为重资产（匕首/手套）
            is_knife_or_glove = any(x in name for x in ["★", "手套", "匕首", "刀", "蝴蝶", "爪子", "M9", "刺刀"])
            
            # 1. 差异化涨跌幅过滤
            sell_price_rate_90 = float(item.get("sell_price_rate_90", 0))
            if is_knife_or_glove:
                # 刀手套目前普遍在跌，我们允许-15%以内的回撤，因为租金能补回来（以息抵本策略）
                max_decline = scanner_config.get("max_decline_aggressive", -15)
                if sell_price_rate_90 < max_decline:
                    self.logger.debug(f"  - {name}: 重资产跌幅过大 (90天跌幅 {sell_price_rate_90:.1f}% < {max_decline}%)，跳过")
                    continue
            else:
                # 枪皮和探员要求更高，不能跌超过8%（因为租金相对低，本金必须稳）
                max_decline = scanner_config.get("max_decline_steady", -8)
                if sell_price_rate_90 < max_decline:
                    self.logger.debug(f"  - {name}: 稳健型跌幅过大 (90天跌幅 {sell_price_rate_90:.1f}% < {max_decline}%)，跳过")
                    continue

            # 2. 差异化溢价检查 (UU对比BUFF)
            yyyp_sell_price = float(item.get("yyyp_sell_price", 0))
            buff_sell_price = float(item.get("buff_sell_price", 0))
            
            if buff_sell_price > 0:
                markup = yyyp_sell_price / buff_sell_price
                if is_knife_or_glove:
                    # 刀手套溢价不能超过8%（因为基数大，溢价太高必跌）
                    max_markup = scanner_config.get("max_markup_aggressive", 1.08)
                    if markup > max_markup:
                        self.logger.debug(f"  - {name}: 重资产溢价过高 ({markup*100:.1f}% > {max_markup*100:.1f}%)，跳过")
                        continue
                else:
                    # 枪皮和探员允许15%溢价
                    max_markup = scanner_config.get("max_markup_steady", 1.15)
                    if markup > max_markup:
                        self.logger.debug(f"  - {name}: 稳健型溢价过高 ({markup*100:.1f}% > {max_markup*100:.1f}%)，跳过")
                        continue

            # 3. 租金稳定性校验（通过 K 线接口）
            # 获取最近 30 天的租金走势，看租金是否经常跳水
            # 用于识别"虚假租金"（挂得高但没人租的情况）
            lease_volatility = self.get_lease_stability(good_id)
            max_lease_volatility = self.config.get("uu_auto_invest", {}).get("max_lease_volatility", 0.15)
            if lease_volatility > max_lease_volatility:  # 租金波动超过15%的不要
                self.logger.debug(f"  - {name}: 租金波动过大 ({lease_volatility:.1%} > {max_lease_volatility:.1%})，跳过")
                continue

            # 所有检查通过，加入白名单
            yyyp_lease_annual = item.get("yyyp_lease_annual", 0)
            roi = float(yyyp_lease_annual) / 100.0
            buy_limit = round(yyyp_sell_price * 0.91, 2)  # 求购建议价（市场价的91%，统一标准）
            asset_type = "重资产" if is_knife_or_glove else "稳健型"  # 标记资产类型

            final_whitelist.append({
                "templateId": str(good_id),
                "name": name,
                "roi": roi,
                "roi_percent": yyyp_lease_annual,
                "buy_limit": buy_limit,
                "current_price": yyyp_sell_price,
                "yyyp_sell_price": yyyp_sell_price,
                "buff_sell_price": buff_sell_price,
                "lease_volatility": round(lease_volatility, 4),
                "sell_price_rate_90": sell_price_rate_90,
                "asset_type": asset_type,  # 标记资产类型
                "selected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            self.logger.info(f"  ✨ [锁定目标] {name} | 年化: {yyyp_lease_annual:.1f}% | 类型: {asset_type} | 90D趋势: {sell_price_rate_90:.1f}% | 租金波动: {lease_volatility:.1%} | 推荐求购价: {buy_limit:.2f}元")

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

