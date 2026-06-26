# -*- coding: utf-8 -*-
"""
悠悠有品购买功能使用示例

使用流程：
1. 在悠悠有品APP上点击商品购买（会创建订单但不付款）
2. 从APP获取订单号(orderNo)和待支付数据编号(waitPaymentDataNo)
3. 运行此脚本完成支付

支付策略：
- 优先使用「仅交易余额」(payWay=17)
- 不足部分使用「可用余额」(payWay=7) 补齐
- 绝不使用支付宝/花呗
"""

import os
import sys

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from uuyoupinapi import UUAccount

# 配置你的悠悠有品Token
UU_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJjNTA4YjYzZDBmOTg0ZjllODA5ZjU0MTA4OTZjYzA5YiIsIm5hbWVpZCI6IjM3NDA4MDIiLCJJZCI6IjM3NDA4MDIiLCJ1bmlxdWVfbmFtZSI6IllQMDAwMzc0MDgwMiIsIk5hbWUiOiJZUDAwMDM3NDA4MDIiLCJuYmYiOjE3Njg0Mzk2MjIsImV4cCI6MTc3MTgyNzIyMiwiaXNzIjoieW91cGluODk4LmNvbSIsInZlcnNpb24iOiJKYVEiLCJkZXZpY2VJZCI6ImFXZytMbnY5YXZrREFMQ3plUUlGd0R5eCIsImF1ZCI6InVzZXIifQ.WK9qRUVk5DzooxwWBNSLikode57QeUdyXqYHNz08awY"
DEVICE_TOKEN = "aWg+Lnv9avkDALCzeQIFwDyx"  # 可选

def main():
    # 初始化账户
    uu = UUAccount(UU_TOKEN, deviceToken=DEVICE_TOKEN)
    print(f"登录成功，昵称: {uu.nickname}")
    print(f"当前余额: {uu.balance} 元")
    
    # =============================================================
    # 方式1：手动输入订单信息
    # =============================================================
    
    # 从APP获取这两个参数（在支付页面抓包可以看到）
    order_no = input("请输入订单号 (orderNo): ").strip()
    wait_payment_data_no = input("请输入待支付数据编号 (waitPaymentDataNo): ").strip()
    payment_amount = input("请输入订单金额 (paymentAmount，如 515.0): ").strip()
    
    # 可选：设置最大支付金额限制
    max_price = input("请输入最大支付金额限制 (留空则不限制): ").strip()
    max_price = float(max_price) if max_price else None
    
    # 执行支付
    result = uu.buy_with_balance(
        order_no=order_no,
        wait_payment_data_no=wait_payment_data_no,
        payment_amount=payment_amount,
        max_price=max_price
    )
    
    if result["success"]:
        print(f"\n✅ 支付成功!")
        print(f"   订单号: {result['data']['orderNo']}")
        print(f"   支付金额: {result['data']['amount']} 元")
        print(f"   当前余额: {uu.balance} 元")
    else:
        print(f"\n❌ 支付失败: {result['message']}")

def demo_query_payment():
    """
    演示：只查询支付方式（不实际支付）
    """
    uu = UUAccount(UU_TOKEN, deviceToken=DEVICE_TOKEN)
    
    order_no = input("请输入订单号: ").strip()
    wait_payment_data_no = input("请输入待支付数据编号: ").strip()
    
    # 查询支付方式
    rsp = uu.query_payment_list(order_no, "0", wait_payment_data_no)
    
    if rsp.get("code") == 0:
        data = rsp["data"]
        print(f"\n订单金额: {data['amount']} 元")
        print("\n可用支付方式:")
        
        for pay in data.get("payList", []):
            checked = "✓" if pay.get("checked") == 1 else " "
            balance = pay.get("balance", "N/A")
            print(f"  [{checked}] {pay['channelName']} (payWay={pay['payWay']}, 余额={balance})")
        
        # 仅交易余额信息
        trade_info = data.get("onlyTradeBalanceInfo")
        if trade_info:
            checked = "✓" if trade_info.get("checked") == 1 else " "
            print(f"  [{checked}] {trade_info['channelName']} (payWay={trade_info['payWay']}, 余额={trade_info['balance']})")
            print(f"      本单最高可用: {trade_info.get('tip', 'N/A')}")
        
        # 计算支付策略
        amount = float(data["amount"])
        mix_pay, channel_id, pay_way, error = uu.calculate_payment_strategy(data, amount)
        
        if error:
            print(f"\n⚠️ 支付策略: {error}")
        else:
            print(f"\n✅ 建议支付策略: {mix_pay}")
    else:
        print(f"查询失败: {rsp.get('msg')}")

def demo_cancel_order():
    """
    演示：取消订单
    """
    uu = UUAccount(UU_TOKEN, deviceToken=DEVICE_TOKEN)
    
    order_no = input("请输入要取消的订单号: ").strip()
    
    rsp = uu.cancel_buy_order(order_no)
    
    if rsp.get("code") == 0:
        print(f"✅ 订单取消成功: {rsp.get('msg')}")
    else:
        print(f"❌ 取消失败: {rsp.get('msg')}")

if __name__ == "__main__":
    print("悠悠有品购买功能")
    print("=" * 40)
    print("1. 支付订单")
    print("2. 查询支付方式（不支付）")
    print("3. 取消订单")
    
    choice = input("\n请选择操作 (1/2/3): ").strip()
    
    if choice == "1":
        main()
    elif choice == "2":
        demo_query_payment()
    elif choice == "3":
        demo_cancel_order()
    else:
        print("无效选择")
