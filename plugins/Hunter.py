"""
CSQAQ 智能选品器 (Hunter)
实现三层过滤选品法，从海量饰品中筛选出稳健型出租资产
"""

import json
import os
import time
import statistics
from typing import Optional, Dict, List, Tuple
from datetime import datetime

import json5
import requests

from utils.logger import PluginLogger, handle_caught_exception


class ItemHunter:
    """
    智能选品器 - 三层过滤法
    1. 回报率过滤：年化收益率在合理区间
    2. 价格趋势过滤：排除持续阴跌的资产
    3. 流动性过滤：确保有足够的租客需求
    4. 稳定性过滤：价格波动率不能太高
    5. 溢价分析：UU价格不能比BUFF高太多
    """

    def __init__(self, config_path: str = "config/config.json5"):
        self.logger = PluginLogger("Hunter")
        self.config_path = config_path
        self.config = self._load_config()
        
        # 从配置读取参数
        invest_config = self.config.get("uu_auto_invest", {})
        
        # 过滤参数
        self.MIN_ROI = invest_config.get("min_roi", 0.25)  # 最小年化回报 (25%)
        self.MAX_ROI = invest_config.get("max_roi", 0.60)  # 最大年化回报 (60%)
        self.MIN_PRICE = invest_config.get("min_price", 100)  # 价格下限
        self.MAX_PRICE = invest_config.get("max_price", 2000)  # 价格上限
        self.MIN_SELL_NUM = invest_config.get("min_on_sale", 100)  # 最小在售数量
        self.MIN_LEASE_NUM = invest_config.get("min_lease_num", 30)  # 最小在租数量
        self.MAX_PRICE_PREMIUM = invest_config.get("max_price_premium", 0.15)  # UU相对BUFF的最大溢价 (15%)
        self.MAX_VOLATILITY = invest_config.get("max_volatility", 0.20)  # 最大价格波动率 (20%)
        self.MIN_TREND_90D = invest_config.get("min_trend_90d", -10)  # 90天最小涨跌幅 (-10%)
        
        # API 配置
        self.api_token = self._get_api_token()
        self.api_base_url = "https://api.csqaq.com/api/v1"
        
        # 输出文件
        self.whitelist_path = "config/invest_whitelist.json"
        
        if not self.api_token:
            self.logger.warning("未配置 csqaq_api_token，Hunter 无法运行")
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

    def _make_api_request(self, endpoint: str, data: dict = None, method: str = "POST") -> Optional[dict]:
        """
        发送 API 请求
        :param endpoint: API 端点路径
        :param data: 请求数据
        :param method: 请求方法 (GET/POST)
        :return: API 响应数据
        """
        if not self.api_token:
            return None

        url = f"{self.api_base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "ApiToken": self.api_token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, params=data, timeout=15)
            else:
                resp = requests.post(url, headers=headers, json=data, timeout=15)

            if resp.status_code == 401:
                self.logger.error("API返回401未授权错误，请检查 csqaq_api_token 和 IP 白名单")
                return None

            if resp.status_code != 200:
                self.logger.error(f"API请求失败: HTTP {resp.status_code}")
                return None

            result = resp.json()
            code = result.get("code")
            
            if code not in [200, 201]:
                msg = result.get("msg", "未知错误")
                self.logger.error(f"API返回错误: {msg} (code: {code})")
                return None

            return result.get("data")

        except Exception as e:
            self.logger.error(f"API请求异常: {e}")
            return None

    def get_rank_list(self, page_size: int = 200) -> List[dict]:
        """
        获取排行榜数据（接口1）
        :param page_size: 获取数量
        :return: 饰品列表
        """
        self.logger.info(f"正在获取排行榜数据（前 {page_size} 名）...")
        
        request_data = {
            "page_index": 1,
            "page_size": min(page_size, 500),
            "search": "",
            "filter": {
                "排序": ["租赁_短租收益率(年化)"],
                "价格最低价": self.MIN_PRICE,
                "价格最高价": self.MAX_PRICE,
                "在售最少": self.MIN_SELL_NUM,
            },
            "show_recently_price": True
        }

        # 遵守频率限制
        time.sleep(1)
        
        data = self._make_api_request("/info/get_rank_list", request_data)
        if not data:
            return []

        items = data.get("data", [])
        self.logger.info(f"获取到 {len(items)} 个候选饰品")
        return items

    def get_item_detail(self, good_id: int) -> Optional[dict]:
        """
        获取饰品详情（接口3）
        :param good_id: 饰品ID
        :return: 饰品详情数据
        """
        request_data = {"good_id": good_id}
        
        # 遵守频率限制
        time.sleep(0.5)
        
        return self._make_api_request("/info/get_good", request_data, method="GET")

    def get_chart_data(self, good_id: int, period: str = "90d") -> Optional[dict]:
        """
        获取K线图表数据（接口2）
        :param good_id: 饰品ID
        :param period: 时间周期 (7d/30d/90d/180d)
        :return: 图表数据
        """
        request_data = {
            "good_id": good_id,
            "period": period
        }
        
        # 遵守频率限制
        time.sleep(0.5)
        
        return self._make_api_request("/info/get_chart", request_data, method="GET")

    def calculate_stability_score(self, chart_data: dict) -> Tuple[float, str]:
        """
        计算价格稳定性得分
        :param chart_data: K线图表数据
        :return: (得分, 原因)
        """
        if not chart_data:
            return 0, "缺少K线数据"

        # 提取价格数据
        price_data = chart_data.get("chart_data", {}).get("price", {})
        prices_90d = price_data.get("90d", [])
        
        if not prices_90d or len(prices_90d) < 10:
            return 0, "历史数据不足"

        # 提取价格值
        prices = []
        for item in prices_90d:
            if isinstance(item, dict):
                price = item.get("value") or item.get("price")
                if price:
                    prices.append(float(price))
            elif isinstance(item, (int, float)):
                prices.append(float(item))

        if len(prices) < 10:
            return 0, "有效价格数据不足"

        # 计算波动率：标准差/均值
        avg_price = statistics.mean(prices)
        if avg_price == 0:
            return 0, "平均价格为0"

        try:
            std_dev = statistics.stdev(prices) if len(prices) > 1 else 0
            volatility = std_dev / avg_price
        except:
            # 如果计算标准差失败，使用简易方法：(最高-最低)/均价
            volatility = (max(prices) - min(prices)) / avg_price

        # 波动率 > 20% 的直接 pass
        if volatility > self.MAX_VOLATILITY:
            return 0, f"价格波动率过高 ({volatility*100:.1f}%)"

        # 返回得分：波动率越低分越高
        score = max(0, 100 - (volatility * 500))  # 波动率1%扣5分
        return score, f"波动率 {volatility*100:.1f}%"

    def analyze_item(self, rank_item: dict, detail_data: Optional[dict], chart_data: Optional[dict]) -> Tuple[Optional[dict], str]:
        """
        三合一分析逻辑
        :param rank_item: 排行榜数据
        :param detail_data: 详情数据
        :param chart_data: K线数据
        :return: (合格项数据, 原因)
        """
        name = rank_item.get("name", "未知")
        item_id = rank_item.get("id")
        good_id = rank_item.get("good_id") or item_id

        # 1. 回报率过滤
        yyyp_lease_annual = rank_item.get("yyyp_lease_annual", 0)
        if not yyyp_lease_annual:
            return None, "缺少年化收益率数据"
        
        roi = float(yyyp_lease_annual) / 100.0
        if not (self.MIN_ROI <= roi <= self.MAX_ROI):
            return None, f"ROI不达标 ({roi*100:.1f}%，要求 {self.MIN_ROI*100:.1f}%-{self.MAX_ROI*100:.1f}%)"

        # 2. 价格趋势过滤
        sell_price_rate_90 = rank_item.get("sell_price_rate_90", 0)
        if sell_price_rate_90 < self.MIN_TREND_90D:
            return None, f"处于中长期下降通道 (90天跌幅 {sell_price_rate_90:.1f}%)"

        # 3. 流动性过滤 - 在售数量
        yyyp_sell_num = int(rank_item.get("yyyp_sell_num", 0))
        if yyyp_sell_num < self.MIN_SELL_NUM:
            return None, f"在售数量不足 ({yyyp_sell_num} < {self.MIN_SELL_NUM})"

        # 4. 流动性过滤 - 在租数量
        yyyp_lease_num = int(rank_item.get("yyyp_lease_num", 0))
        if detail_data:
            # 从详情数据中获取更准确的在租数量
            goods_info = detail_data.get("goods_info", {})
            yyyp_lease_num = int(goods_info.get("yyyp_lease_num", yyyp_lease_num))

        if yyyp_lease_num < self.MIN_LEASE_NUM:
            return None, f"在租数量不足 ({yyyp_lease_num} < {self.MIN_LEASE_NUM})"

        # 在租/在售比检查
        if yyyp_sell_num > 0:
            lease_sell_ratio = yyyp_lease_num / yyyp_sell_num
            if lease_sell_ratio < 0.1:
                return None, f"出租流动性差 (在租/在售比 {lease_sell_ratio*100:.1f}% < 10%)"

        # 5. 稳定性终审
        stability_score, stability_reason = self.calculate_stability_score(chart_data)
        if stability_score < 80:
            return None, f"价格波动太剧烈 ({stability_reason})"

        # 6. 溢价分析
        yyyp_sell_price = float(rank_item.get("yyyp_sell_price", 0))
        buff_sell_price = float(rank_item.get("buff_sell_price", 0))
        
        if buff_sell_price > 0:
            premium = (yyyp_sell_price - buff_sell_price) / buff_sell_price
            if premium > self.MAX_PRICE_PREMIUM:
                return None, f"UU溢价过高 ({premium*100:.1f}% > {self.MAX_PRICE_PREMIUM*100:.1f}%)，易跌"

        # 7. 短期价格稳定性检查（7天涨跌幅）
        sell_price_rate_7 = rank_item.get("sell_price_rate_7", 0)
        if sell_price_rate_7 < -3 or sell_price_rate_7 > 3:
            return None, f"短期价格波动过大 (7天涨跌幅 {sell_price_rate_7:.1f}%)"

        # 所有检查通过，返回合格项
        target_buy_price = round(yyyp_sell_price * 0.90, 2)  # 求购价定在9折
        
        return {
            "id": item_id,
            "good_id": good_id,
            "name": name,
            "roi": roi,
            "roi_percent": yyyp_lease_annual,
            "stability_score": round(stability_score, 2),
            "yyyp_sell_price": yyyp_sell_price,
            "yyyp_buy_price": rank_item.get("yyyp_buy_price", 0),
            "target_buy_price": target_buy_price,
            "yyyp_sell_num": yyyp_sell_num,
            "yyyp_lease_num": yyyp_lease_num,
            "lease_sell_ratio": round(yyyp_lease_num / yyyp_sell_num if yyyp_sell_num > 0 else 0, 3),
            "sell_price_rate_90": sell_price_rate_90,
            "sell_price_rate_7": sell_price_rate_7,
            "market_hash_name": rank_item.get("market_hash_name", ""),
            "selected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }, "合格"

    def hunt(self) -> List[dict]:
        """
        执行选品流程
        :return: 合格饰品列表
        """
        self.logger.info("=" * 60)
        self.logger.info("开始执行智能选品 (三层过滤法)")
        self.logger.info("=" * 60)

        # 第一步：海选 - 获取排行榜
        rank_items = self.get_rank_list(page_size=200)
        if not rank_items:
            self.logger.error("无法获取排行榜数据，选品终止")
            return []

        self.logger.info(f"海选阶段：获取到 {len(rank_items)} 个候选饰品")

        # 第二步：精选 - 对每个饰品进行详细分析
        qualified_items = []
        total_items = len(rank_items)
        
        for index, rank_item in enumerate(rank_items):
            item_name = rank_item.get("name", "未知")
            good_id = rank_item.get("good_id") or rank_item.get("id")
            
            self.logger.info(f"[{index+1}/{total_items}] 分析: {item_name}")

            # 获取详情和K线数据
            detail_data = None
            chart_data = None
            
            if good_id:
                detail_data = self.get_item_detail(good_id)
                chart_data = self.get_chart_data(good_id, period="90d")

            # 执行分析
            result, reason = self.analyze_item(rank_item, detail_data, chart_data)
            
            if result:
                qualified_items.append(result)
                self.logger.info(f"  ✅ 合格！ROI: {result['roi_percent']:.1f}%, 稳定性: {result['stability_score']:.1f}, 推荐求购价: {result['target_buy_price']:.2f}")
            else:
                self.logger.debug(f"  ❌ 淘汰: {reason}")

            # 避免请求过快
            if (index + 1) % 10 == 0:
                self.logger.info(f"已分析 {index+1}/{total_items} 个饰品，当前合格: {len(qualified_items)} 个")
                time.sleep(2)  # 每10个休息2秒

        # 第三步：排序和筛选
        # 按稳定性得分和ROI综合排序
        qualified_items.sort(key=lambda x: (x["stability_score"] * 0.6 + x["roi"] * 100 * 0.4), reverse=True)
        
        # 只保留前10个最优质的
        final_items = qualified_items[:10]

        self.logger.info("=" * 60)
        self.logger.info(f"选品完成！共筛选出 {len(final_items)} 个优质饰品")
        self.logger.info("=" * 60)

        return final_items

    def save_whitelist(self, items: List[dict]):
        """
        保存白名单到文件
        :param items: 合格饰品列表
        """
        whitelist_data = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": len(items),
            "items": items
        }

        try:
            os.makedirs(os.path.dirname(self.whitelist_path), exist_ok=True)
            with open(self.whitelist_path, "w", encoding="utf-8") as f:
                json.dump(whitelist_data, f, ensure_ascii=False, indent=2)
            
            self.logger.info(f"白名单已保存到: {self.whitelist_path}")
            self.logger.info(f"共 {len(items)} 个优质饰品已入库")
        except Exception as e:
            self.logger.error(f"保存白名单失败: {e}")

    def run(self):
        """执行完整的选品流程"""
        if not self.api_token:
            self.logger.error("未配置 API Token，无法运行")
            return

        try:
            # 执行选品
            qualified_items = self.hunt()
            
            if qualified_items:
                # 保存白名单
                self.save_whitelist(qualified_items)
                
                # 打印摘要
                self.logger.info("\n" + "=" * 60)
                self.logger.info("选品摘要")
                self.logger.info("=" * 60)
                for i, item in enumerate(qualified_items, 1):
                    self.logger.info(f"{i}. {item['name']}")
                    self.logger.info(f"   ROI: {item['roi_percent']:.1f}% | "
                                   f"稳定性: {item['stability_score']:.1f} | "
                                   f"推荐求购价: {item['target_buy_price']:.2f}元")
                self.logger.info("=" * 60)
            else:
                self.logger.warning("未找到符合条件的饰品，请调整筛选参数")
                
        except Exception as e:
            handle_caught_exception(e, "Hunter")
            self.logger.error("选品过程出现异常")


def main():
    """主函数 - 独立运行"""
    hunter = ItemHunter()
    hunter.run()


if __name__ == "__main__":
    main()

