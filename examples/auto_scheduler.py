# -*- coding: utf-8 -*-
"""
每日定时扫货 + MuMu自动购买 联动调度器

流程:
  1. 每天 12:00 自动运行 Scanner 扫货
  2. 扫货完成后读取白名单，筛选 A 级饰品
  3. 通过悠悠有品 API 获取实时余额
  4. 检查是否有 buy_limit <= 余额 的 A 级饰品
  5. 如果有 → 连接 MuMu 模拟器自动搜索购买（最多1个）
  6. 如果没有 → 跳过购买

使用方法:
  python examples/auto_scheduler.py          # 定时模式（每天12:00执行）
  python examples/auto_scheduler.py --now    # 立即执行一次
"""

import os
import sys
import json
import time
import glob
import schedule
from datetime import datetime

# 添加项目根目录到路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


# ============================================================
# 配置
# ============================================================
DEFAULT_CONFIG = {
    "run_time": "12:00",       # 每天执行时间
    "max_buy_per_run": 1,      # 每次最多购买几个
    "only_tier_a": True,       # 只购买 A 级饰品
    "mumu_port": 16384,        # MuMu 模拟器端口
    "scanner_before_buy": True # 购买前先扫货
}


def load_config():
    """加载配置"""
    try:
        import json5
        with open("config/config.json5", "r", encoding="utf-8") as f:
            config = json5.load(f)
        
        # 合并默认配置
        mumu_config = config.get("uu_auto_invest", {}).get("mumu_auto_buy", {})
        merged = {**DEFAULT_CONFIG, **mumu_config}
        
        # 也保留完整config供Scanner使用
        merged["_full_config"] = config
        return merged
    except Exception as e:
        print(f"⚠️ 加载配置失败: {e}，使用默认配置")
        return DEFAULT_CONFIG


def get_balance():
    """
    通过悠悠有品 API 获取实时余额
    :return: 余额（float），失败返回 0
    """
    print("\n📊 获取悠悠有品余额...")
    
    try:
        import uuyoupinapi
        
        # 查找 token 文件
        token_files = glob.glob("config/uu_token*.txt")
        if not token_files:
            print("❌ 未找到 uu_token 文件")
            return 0
        
        # 优先使用非 test 的 token
        token_file = None
        for f in token_files:
            if "test" not in f:
                token_file = f
                break
        if not token_file:
            token_file = token_files[0]
        
        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
        
        if not token:
            print("❌ Token 文件为空")
            return 0
        
        # 创建 UU 账户并获取余额
        uu = uuyoupinapi.UUAccount(token)
        balance = uu.refresh_balance()
        print(f"✅ 当前余额: ¥{balance:.2f}")
        return balance
        
    except Exception as e:
        print(f"❌ 获取余额失败: {e}")
        return 0


def run_scanner():
    """
    运行 Scanner 扫货
    :return: True=成功, False=失败
    """
    print("\n" + "=" * 60)
    print("🔍 开始运行 Scanner 扫货...")
    print("=" * 60)
    
    try:
        from plugins.Scanner import CSQAQScanner
        
        scanner = CSQAQScanner()
        scanner.run()
        
        print("✅ Scanner 扫货完成")
        return True
        
    except Exception as e:
        print(f"❌ Scanner 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def load_whitelist():
    """
    读取白名单（英文版，程序用）
    :return: 白名单列表
    """
    whitelist_path = "config/whitelist.json"
    
    if not os.path.exists(whitelist_path):
        print("❌ 白名单文件不存在，请先运行 Scanner")
        return []
    
    try:
        with open(whitelist_path, "r", encoding="utf-8") as f:
            items = json.load(f)
        print(f"✅ 已加载白名单: {len(items)} 个饰品")
        return items
    except Exception as e:
        print(f"❌ 加载白名单失败: {e}")
        return []


def filter_affordable_items(whitelist, balance, only_tier_a=True):
    """
    筛选买得起的饰品
    :param whitelist: 白名单列表
    :param balance: 当前余额
    :param only_tier_a: 是否只看A级
    :return: 可购买的饰品列表（按ROI降序）
    """
    affordable = []
    
    for item in whitelist:
        tier = item.get("tier", "C")
        name = item.get("name", "未知")
        buy_limit = float(item.get("buy_limit", 0) or 0)
        yyyp_price = float(item.get("yyyp_sell_price", 0) or 0)
        roi = item.get("roi_percent", 0)
        
        # 只看指定等级
        if only_tier_a and tier not in ["S", "A"]:
            continue
        
        # 用推荐求购价判断是否买得起
        # 如果没有buy_limit，用悠悠有品售价
        price_to_check = buy_limit if buy_limit > 0 else yyyp_price
        
        if price_to_check <= 0:
            continue
        
        if price_to_check <= balance:
            affordable.append({
                "name": name,
                "tier": tier,
                "buy_limit": buy_limit,
                "yyyp_sell_price": yyyp_price,
                "roi_percent": roi,
                "price_to_check": price_to_check,
            })
    
    # 按年化收益率降序排列
    affordable.sort(key=lambda x: x["roi_percent"], reverse=True)
    return affordable


def run_mumu_buy(items, mumu_port=16384, max_buy=1):
    """
    通过 MuMu 模拟器自动购买
    :param items: 要购买的饰品列表
    :param mumu_port: MuMu 端口
    :param max_buy: 最多购买几个
    :return: 购买成功数量
    """
    print("\n" + "=" * 60)
    print("🎮 启动 MuMu 自动购买...")
    print("=" * 60)
    
    try:
        # 导入 mumu_auto_buy 模块
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "examples"))
        from mumu_auto_buy import UUAutoBuyer
        
        buyer = UUAutoBuyer(mumu_port=mumu_port)
        
        if not buyer.connect():
            print("❌ 无法连接 MuMu 模拟器")
            return 0
        
        success_count = 0
        
        for i, item in enumerate(items[:max_buy]):
            name = item["name"]
            price = item["price_to_check"]
            roi = item["roi_percent"]
            
            print(f"\n{'='*50}")
            print(f"[{i+1}/{min(len(items), max_buy)}] 购买: {name}")
            print(f"  推荐价: ¥{price:.2f}  年化: {roi:.2f}%")
            print(f"{'='*50}")
            
            result = buyer.search_and_buy(name)
            
            if result:
                success_count += 1
                print(f"✅ 购买成功: {name}")
            else:
                print(f"⚠️ 购买失败或已取消: {name}")
            
            # 回到首页准备下一个
            buyer.go_back()
            time.sleep(2)
        
        return success_count
        
    except Exception as e:
        print(f"❌ MuMu 购买失败: {e}")
        import traceback
        traceback.print_exc()
        return 0


def daily_job():
    """每日定时任务"""
    config = load_config()
    
    print("\n" + "=" * 60)
    print(f"🕐 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 每日任务开始")
    print("=" * 60)
    
    # 1. 运行 Scanner 扫货（可选）
    if config.get("scanner_before_buy", True):
        scan_ok = run_scanner()
        if not scan_ok:
            print("⚠️ 扫货失败，尝试使用现有白名单继续...")
    
    # 2. 读取白名单
    whitelist = load_whitelist()
    if not whitelist:
        print("❌ 没有可用的白名单数据，任务结束")
        return
    
    # 3. 获取实时余额
    balance = get_balance()
    if balance <= 0:
        print("❌ 余额获取失败或为0，任务结束")
        return
    
    # 4. 筛选买得起的 A 级饰品
    only_a = config.get("only_tier_a", True)
    affordable = filter_affordable_items(whitelist, balance, only_a)
    
    tier_label = "A级" if only_a else "S/A/B级"
    
    if not affordable:
        # 显示为什么买不起
        all_a_items = [
            item for item in whitelist 
            if item.get("tier", "C") in (["S", "A"] if only_a else ["S", "A", "B"])
        ]
        if all_a_items:
            min_price = min(float(item.get("buy_limit", 9999) or 9999) for item in all_a_items)
            print(f"\n💰 所有{tier_label}饰品最低求购价 ¥{min_price:.2f}，当前余额 ¥{balance:.2f}")
            print(f"❌ 余额不足，无法购买任何{tier_label}饰品，任务结束")
        else:
            print(f"❌ 白名单中没有{tier_label}饰品，任务结束")
        return
    
    # 5. 显示可购买列表
    max_buy = config.get("max_buy_per_run", 1)
    print(f"\n✅ 发现 {len(affordable)} 个买得起的{tier_label}饰品（余额: ¥{balance:.2f}）：")
    for i, item in enumerate(affordable):
        flag = "→" if i < max_buy else " "
        print(f"  {flag} {item['name']}  求购:¥{item['price_to_check']:.2f}  年化:{item['roi_percent']:.2f}%")
    
    print(f"\n将购买前 {max_buy} 个（按年化收益率排序）")
    
    # 6. 执行 MuMu 自动购买
    mumu_port = config.get("mumu_port", 16384)
    bought = run_mumu_buy(affordable, mumu_port=mumu_port, max_buy=max_buy)
    
    # 7. 汇报结果
    print("\n" + "=" * 60)
    print(f"📋 任务完成！成功购买 {bought}/{min(len(affordable), max_buy)} 个")
    print(f"🕐 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 任务结束")
    print("=" * 60)


def main():
    """主函数"""
    print("=" * 60)
    print("悠悠有品 每日扫货+自动购买 调度器")
    print("=" * 60)
    
    config = load_config()
    run_time = config.get("run_time", "12:00")
    
    # 检查是否要立即执行
    if "--now" in sys.argv:
        print("\n⏩ 立即执行模式")
        daily_job()
        return
    
    # 定时模式
    print(f"\n⏰ 定时模式：每天 {run_time} 执行")
    print(f"📝 配置：每次最多买 {config.get('max_buy_per_run', 1)} 个")
    print(f"🔍 扫货前置：{'是' if config.get('scanner_before_buy', True) else '否'}")
    print(f"📡 MuMu 端口：{config.get('mumu_port', 16384)}")
    print("\n等待中... (按 Ctrl+C 退出)")
    
    # 设置定时任务
    schedule.every().day.at(run_time).do(daily_job)
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)  # 每30秒检查一次
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断，调度器已停止")


if __name__ == "__main__":
    main()
