# -*- coding: utf-8 -*-
"""
MuMu模拟器自动购买脚本

使用纯ADB命令控制模拟器，实现悠悠有品自动购买流程。

前置条件：
1. 安装MuMu模拟器并打开悠悠有品APP
2. 登录悠悠有品账号
3. 确保ADB可用（MuMu自带ADB）

使用方法：
1. 在MuMu中打开悠悠有品APP
2. 搜索到想买的商品（在商品详情页）
3. 运行此脚本，自动完成购买

MuMu ADB端口：
- MuMu 12：默认端口 16384
- MuMu 6/X：默认端口 7555
"""

import os
import sys
import subprocess
import time
import re

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


class MuMuController:
    """MuMu模拟器控制器"""
    
    # MuMu ADB常见路径
    MUMU_ADB_PATHS = [
        r"C:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe",  # MuMu 12 新版
        r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",  # MuMu 12 主程序
        r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"D:\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Program Files\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
        r"D:\Program Files\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
    ]
    
    def __init__(self, port=16384, adb_path=None):
        """
        初始化MuMu控制器
        :param port: MuMu ADB端口（MuMu 12默认16384，MuMu 6默认7555）
        :param adb_path: ADB可执行文件路径（默认自动查找）
        """
        self.device = f"127.0.0.1:{port}"
        self.adb_path = adb_path or self._find_adb()
        self.screen_width = 0
        self.screen_height = 0
        
    def _find_adb(self):
        """自动查找ADB路径"""
        # 首先尝试常见的MuMu安装路径
        for path in self.MUMU_ADB_PATHS:
            if os.path.exists(path):
                print(f"找到MuMu ADB: {path}")
                return path
        
        # 尝试系统PATH中的adb
        try:
            result = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print("使用系统PATH中的ADB")
                return "adb"
        except:
            pass
        
        print("⚠️ 未找到ADB，请手动指定路径")
        return None
        
    def _run_adb(self, args, timeout=10):
        """执行ADB命令"""
        if not self.adb_path:
            print("ADB路径未设置")
            return None
        cmd = [self.adb_path] + args
        try:
            # 使用utf-8编码并忽略错误，避免GBK解码问题
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                timeout=timeout,
                encoding='utf-8',
                errors='ignore'
            )
            return result.stdout.strip() if result.stdout else ""
        except subprocess.TimeoutExpired:
            print(f"ADB命令超时: {' '.join(args)}")
            return None
        except Exception as e:
            print(f"ADB命令失败: {e}")
            return None
    
    def connect(self):
        """连接到MuMu模拟器"""
        print(f"正在连接MuMu模拟器 ({self.device})...")
        result = self._run_adb(["connect", self.device])
        if result and ("connected" in result or "already connected" in result):
            print(f"✅ 连接成功: {result}")
            # 连接成功后获取屏幕分辨率
            self._get_screen_size()
            return True
        else:
            print(f"❌ 连接失败: {result}")
            return False
    
    def _get_screen_size(self):
        """获取屏幕分辨率"""
        result = self.shell("wm size")
        if result:
            # 输出格式: Physical size: 1080x1920
            import re
            match = re.search(r'(\d+)x(\d+)', result)
            if match:
                self.screen_width = int(match.group(1))
                self.screen_height = int(match.group(2))
                print(f"屏幕分辨率: {self.screen_width}x{self.screen_height}")
                return
        # 默认分辨率
        self.screen_width = 1080
        self.screen_height = 1920
        print(f"使用默认分辨率: {self.screen_width}x{self.screen_height}")
    
    def disconnect(self):
        """断开连接"""
        self._run_adb(["disconnect", self.device])
    
    def shell(self, cmd, timeout=10):
        """执行shell命令"""
        return self._run_adb(["-s", self.device, "shell"] + cmd.split(), timeout=timeout)
    
    def tap(self, x, y):
        """点击屏幕坐标"""
        print(f"  → 点击 ({x}, {y})")
        self.shell(f"input tap {x} {y}")
        time.sleep(0.5)  # 等待响应
    
    def swipe(self, x1, y1, x2, y2, duration=300):
        """滑动屏幕"""
        print(f"  → 滑动 ({x1},{y1}) -> ({x2},{y2})")
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration}")
        time.sleep(0.5)
    
    def text(self, content):
        """输入文本（仅支持英文和数字）"""
        # 处理空格和特殊字符
        content = content.replace(" ", "%s").replace("&", "\\&")
        self.shell(f"input text {content}")
        time.sleep(0.3)
    
    def clear_input_field(self):
        """清空输入框内容"""
        print("  → 清空输入框")
        # 方法1: Move to End + Long Delete
        self.keyevent("123") # MOVE_END
        for _ in range(3): # 多次删除防止未清空
             # 模拟长按删除 (KEYCODE_DEL = 67)
             # 也可以直接发送多次删除键
             self.shell("input keyevent 67 67 67 67 67 67 67 67 67 67") 
        time.sleep(0.5)
    
    def set_pc_clipboard(self, text):
        """设置PC端剪贴板（用于模拟器同步）"""
        try:
            import ctypes
            from ctypes import wintypes
            
            # Windows API
            OpenClipboard = ctypes.windll.user32.OpenClipboard
            EmptyClipboard = ctypes.windll.user32.EmptyClipboard
            SetClipboardData = ctypes.windll.user32.SetClipboardData
            CloseClipboard = ctypes.windll.user32.CloseClipboard
            GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
            GlobalLock = ctypes.windll.kernel32.GlobalLock
            GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
            
            GMEM_MOVEABLE = 0x0002
            CF_UNICODETEXT = 13
            
            # 编码为utf-16le并添加结束符
            data = text.encode('utf-16le') + b'\x00\x00'
            
            OpenClipboard(0)
            try:
                EmptyClipboard()
                hGlobal = GlobalAlloc(GMEM_MOVEABLE, len(data))
                if hGlobal:
                    lpGlobal = GlobalLock(hGlobal)
                    if lpGlobal:
                        ctypes.memmove(lpGlobal, data, len(data))
                        GlobalUnlock(hGlobal)
                        SetClipboardData(CF_UNICODETEXT, hGlobal)
            finally:
                CloseClipboard()
            return True
        except Exception as e:
            print(f"设置PC剪贴板失败: {e}")
            return False

    def input_chinese(self, content):
        """输入中文文本（通过剪贴板同步方式）
        MuMu模拟器会自动同步PC剪贴板，所以我们设置PC剪贴板然后发送粘贴键即可
        """
        print(f"  → 输入文本: '{content}'")
        
        # 1. 设置PC剪贴板
        if self.set_pc_clipboard(content):
            print("    (已同步到PC剪贴板)")
        
        # 2. 尝试 ADBKeyBoard 广播 (双保险)
        escaped = content.replace('"', '\\"').replace("'", "\\'")
        self.shell(f'am broadcast -a clipper.set -e text "{escaped}"')
        time.sleep(0.3)
        
        # 3. 发送粘贴命令 (Ctrl+V)
        # 尝试多种粘贴快捷键
        self.shell('input keyevent 279')  # KEYCODE_PASTE
        time.sleep(0.5)
    
    def keyevent(self, key):
        """发送按键事件"""
        self.shell(f"input keyevent {key}")
        time.sleep(0.3)
    
    def back(self):
        """返回键"""
        self.keyevent("KEYCODE_BACK")
    
    def home(self):
        """Home键"""
        self.keyevent("KEYCODE_HOME")
    
    def screenshot(self, local_path="screenshot.png"):
        """截图并下载到本地"""
        remote_path = "/sdcard/screenshot.png"
        self.shell(f"screencap -p {remote_path}")
        self._run_adb(["-s", self.device, "pull", remote_path, local_path])
        print(f"截图已保存: {local_path}")
        return local_path
    
    def get_current_activity(self):
        """获取当前Activity"""
        result = self.shell("dumpsys activity activities | grep mResumedActivity")
        return result
    
    def find_text_on_screen(self, text):
        """
        查找屏幕上的文本（使用UI dump）
        返回文本的坐标，如果找不到返回None
        """
        # 获取UI层次结构
        self.shell("uiautomator dump /sdcard/ui.xml")
        xml_content = self.shell("cat /sdcard/ui.xml")
        
        if not xml_content or text not in xml_content:
            return None
        
        # 使用正则提取bounds
        pattern = f'text="{re.escape(text)}"[^>]*bounds="\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]"'
        match = re.search(pattern, xml_content)
        
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            # 返回中心坐标
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        
        return None
    
    def click_text(self, text, timeout=5):
        """
        点击包含指定文本的元素
        :param text: 要查找的文本
        :param timeout: 超时时间（秒）
        :return: 是否成功
        """
        print(f"查找并点击: '{text}'")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            coords = self.find_text_on_screen(text)
            if coords:
                self.tap(coords[0], coords[1])
                return True
            time.sleep(0.5)
        
        print(f"  ✗ 未找到文本: '{text}'")
        return False


class UUAutoBuyer:
    """悠悠有品自动购买器"""
    
    # 悠悠有品APP中常见按钮的坐标（基于用户截图分析，约600x1200分辨率）
    # 实际使用时可能需要根据屏幕分辨率调整
    COORDS = {
        "buy_button": (540, 490),      # 「购买」按钮（商品列表中第一个）
        "confirm_pay": (400, 1150),    # 「确认付款」按钮（支付弹窗底部）
        "balance_option": (400, 505),  # 「可用余额」选项
        "close_popup": (780, 275),     # 关闭弹窗X按钮
    }
    
    def __init__(self, mumu_port=16384):
        """
        初始化自动购买器
        :param mumu_port: MuMu模拟器ADB端口
        """
        self.controller = MuMuController(port=mumu_port)
    
    def connect(self):
        """连接模拟器"""
        return self.controller.connect()
    
    def check_balance_payment_available(self):
        """
        检查支付页面是否有余额支付选项
        安全原则：只有明确检测到余额选项才返回True，否则一律取消
        :return: True=明确有余额选项, False=没有或不确定
        """
        print("检查支付方式...")
        
        # 等待弹窗加载（给弹窗更多时间加载完整）
        time.sleep(2)
        
        # 获取UI结构
        self.controller.shell("uiautomator dump /sdcard/ui.xml")
        xml_content = self.controller.shell("cat /sdcard/ui.xml")
        
        if not xml_content:
            print("❌ 无法获取UI结构，为安全起见取消支付")
            return False  # 安全优先：无法确认就取消
        
        # ===== 调试输出：保存XML到本地方便分析 =====
        try:
            with open("debug_ui_dump.xml", "w", encoding="utf-8") as f:
                f.write(xml_content)
            print(f"  [调试] UI XML已保存到 debug_ui_dump.xml ({len(xml_content)} 字符)")
        except:
            pass
        
        # 提取所有text属性，方便查看
        import re
        texts = re.findall(r'text="([^"]+)"', xml_content)
        if texts:
            print(f"  [调试] 检测到的文本: {texts}")
        else:
            print("  [调试] 未检测到任何文本内容")
        # ===== 调试输出结束 =====
        
        # 检查是否有余额选项（扩大关键词匹配范围）
        balance_keywords = ["可用余额", "仅交易余额", "余额支付", "余额", "交易余额"]
        has_balance = any(kw in xml_content for kw in balance_keywords)
        has_alipay = "支付宝" in xml_content or "花呗" in xml_content
        
        if has_balance:
            print("✅ 检测到余额支付选项")
            return True
        elif has_alipay:
            print("⚠️ 只检测到支付宝/花呗，没有余额选项")
            return False
        else:
            # 安全原则：无法确认有余额就取消
            print("❌ 未检测到任何支付选项，为安全起见取消支付")
            return False
    
    def cancel_payment(self):
        """取消当前支付 - 点击X → 点击"残忍取消"
        流程: X按钮 → 弹出"确认要放弃付款吗" → 点击"残忍取消"
        坐标通过 position_tool.py 实测确认
        """
        print("取消支付...")
        
        # 第一步：点击X按钮关闭支付弹窗
        screen_w = self.controller.screen_width or 1080
        screen_h = self.controller.screen_height or 1920
        
        # X按钮实测坐标: 1080x1920下为 (1012, 902)
        x_btn_x = int(screen_w * 0.937)  # 1080 → 1012
        x_btn_y = int(screen_h * 0.47)   # 1920 → 902
        
        print(f"  → 第1步: 点击X按钮 ({x_btn_x}, {x_btn_y})")
        self.controller.tap(x_btn_x, x_btn_y)
        time.sleep(1.5)  # 等待"确认放弃"弹窗出现
        
        # 第二步：点击"残忍取消"按钮
        print("  → 第2步: 点击「残忍取消」")
        if not self.controller.click_text("残忍取消", timeout=3):
            # 兜底：如果文本识别失败，用坐标点击
            # "残忍取消"按钮在弹窗左侧，约 屏幕宽度25%, 高度55%
            fallback_x = int(screen_w * 0.25)
            fallback_y = int(screen_h * 0.55)
            print(f"  → 文本查找失败，使用坐标 ({fallback_x}, {fallback_y})")
            self.controller.tap(fallback_x, fallback_y)
        time.sleep(1)
        
        # 第三步：点击返回按钮"<"回到商品列表
        screen_w = self.controller.screen_width or 1080
        screen_h = self.controller.screen_height or 1920
        
        # 返回按钮"<"实测坐标: 1080x1920下为 (90, 145)
        back_x = int(screen_w * 0.083)   # 1080 → ~90
        back_y = int(screen_h * 0.0755)  # 1920 → ~145
        
        print(f"  → 第3步: 点击返回按钮 ({back_x}, {back_y})")
        self.controller.tap(back_x, back_y)
        time.sleep(1)
        
        # 第四步：点击返回"<"回到首页
        # 实测坐标: 1080x1920下为 (63, 104)
        home_x = int(screen_w * (63/1080))
        home_y = int(screen_h * (104/1920))
        print(f"  → 第4步: 点击返回首页 ({home_x}, {home_y})")
        self.controller.tap(home_x, home_y)
        time.sleep(1)
        
        print("✅ 已取消支付并返回首页")
    
    def buy_current_item(self):
        """
        购买当前页面的商品
        前提：已经在商品在售列表页（有「购买」按钮）
        """
        print("\n" + "=" * 50)
        print("开始自动购买流程")
        print("=" * 50)
        
        # 步骤1：点击「购买」按钮
        print("\n步骤1: 点击「购买」按钮")
        if not self.controller.click_text("购买", timeout=3):
            self.controller.tap(*self.COORDS["buy_button"])
        time.sleep(1.5)
        
        # 步骤2：检查是否有余额支付选项
        print("\n步骤2: 检查支付方式")
        if not self.check_balance_payment_available():
            print("❌ 没有余额支付选项，自动取消!")
            self.cancel_payment()
            self.controller.screenshot("cancelled_buy.png")
            print("\n" + "=" * 50)
            print("已取消购买（余额不足）")
            print("=" * 50)
            return False
        
        # 步骤3：点击「确认付款」按钮
        print("\n步骤3: 点击「确认付款」")
        if not self.controller.click_text("确认付款", timeout=3):
            self.controller.tap(*self.COORDS["confirm_pay"])
        time.sleep(2)
        
        # 步骤4：检查结果
        print("\n步骤4: 检查购买结果")
        self.controller.screenshot("buy_result.png")
        
        print("\n" + "=" * 50)
        print("购买流程完成！请查看 buy_result.png 确认结果")
        print("=" * 50)
        return True
    
    def go_back(self):
        """点击左上角返回按钮"<"回到上一页
        实测坐标: 1080x1920下为 (90, 145)
        """
        screen_w = self.controller.screen_width or 1080
        screen_h = self.controller.screen_height or 1920
        back_x = int(screen_w * 0.083)
        back_y = int(screen_h * 0.0755)
        print(f"  → 点击返回 ({back_x}, {back_y})")
        self.controller.tap(back_x, back_y)
        time.sleep(1)

    def search_and_buy(self, keyword, max_price=None):
        """
        从首页搜索商品并购买
        流程: 首页点击"搜索饰品"框 → 输入关键词 → 点击搜索 → 点击第一个商品 → 购买
        :param keyword: 搜索关键词 (支持中文)
        :param max_price: 最高价格限制
        """
        print("\n" + "=" * 50)
        print(f"搜索并购买: {keyword}")
        print("=" * 50)
        
        screen_w = self.controller.screen_width or 1080
        screen_h = self.controller.screen_height or 1920
        
        # 步骤1：在首页点击"搜索饰品"搜索框
        print("\n步骤1: 点击搜索框")
        # 首页搜索框位置: 约 屏幕中间偏左，顶部
        # 首先尝试通过文本查找
        if not self.controller.click_text("搜索饰品", timeout=3):
            # 兜底坐标: 1080x1920下搜索框大约在 (270, 148)
            search_box_x = int(screen_w * 0.25)
            search_box_y = int(screen_h * 0.077)
            print(f"  → 文本查找失败，使用坐标 ({search_box_x}, {search_box_y})")
            self.controller.tap(search_box_x, search_box_y)
        time.sleep(1.5)  # 等待搜索页面加载
        
        # 步骤2：在搜索页面点击输入框并输入关键词
        print(f"\n步骤2: 输入搜索关键词 '{keyword}'")
        # 搜索页面的输入框位置: 约 (270, 142)
        input_box_x = int(screen_w * 0.25)
        input_box_y = int(screen_h * 0.074)
        self.controller.tap(input_box_x, input_box_y)
        time.sleep(0.5)
        
        # 清空已有内容
        self.controller.clear_input_field()
        
        # 输入中文关键词
        self.controller.input_chinese(keyword)
        time.sleep(1.0) # 等待输入法响应
        
        # 步骤3：点击"搜索"按钮（在搜索页面右上角）
        print("\n步骤3: 点击搜索")
        if not self.controller.click_text("搜索", timeout=3):
            # 兜底: 按回车键搜索
            self.controller.keyevent("KEYCODE_ENTER")
        time.sleep(3)  # 等待搜索结果加载 (稍微延长)
        
        # 步骤4：点击第一个搜索结果（商品）
        print("\n步骤4: 选择第一个商品")
        # 步骤4：点击第一个搜索结果（商品）
        print("\n步骤4: 选择第一个商品")
        # 搜索结果中第一个商品位置
        # 用户实测坐标: (139, 455)
        first_item_x = 139
        first_item_y = 455
        
        # 适配不同分辨率 (假设基于1080x1920)
        if screen_w != 1080 or screen_h != 1920:
             first_item_x = int(screen_w * (139/1080))
             first_item_y = int(screen_h * (455/1920))
        
        print(f"  → 点击第一个商品 ({first_item_x}, {first_item_y})")
        self.controller.tap(first_item_x, first_item_y)
        time.sleep(3)  # 等待商品详情加载 (给多一点时间)
        
        # 步骤5：执行购买流程
        print("\n步骤5: 开始购买")
        return self.buy_current_item()
def load_whitelist():
    """加载白名单配置"""
    import json
    # 支持从多个路径加载
    possible_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "whitelist_zh.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "whitelist_zh.json"),
        "config/whitelist_zh.json",
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                print(f"✅ 已加载白名单: {path} ({len(items)} 件饰品)")
                return items
            except Exception as e:
                print(f"⚠️ 加载白名单失败: {e}")
    
    print("⚠️ 未找到 config/whitelist_zh.json")
    return []


def main():
    """主函数"""
    print("=" * 60)
    print("悠悠有品 MuMu模拟器自动购买工具")
    print("=" * 60)
    
    # 获取MuMu端口
    port = 16384
    print(f"\n使用MuMu端口: {port}")
    
    # 创建购买器
    buyer = UUAutoBuyer(mumu_port=port)
    
    # 连接模拟器
    if not buyer.connect():
        print("\n无法连接到MuMu模拟器，请检查：")
        print("1. MuMu模拟器是否已启动")
        print("2. 端口号是否正确")
        print("3. 是否已开启ADB调试")
        return
    
    # 加载白名单
    whitelist = load_whitelist()
    
    print("\n请选择操作：")
    print("1. 购买当前页面商品（需先手动打开商品详情页）")
    print("2. 手动输入关键词搜索购买")
    print("3. 从白名单选择饰品搜索购买")
    print("4. 截图当前屏幕")
    
    choice = input("\n请选择 (1/2/3/4): ").strip()
    
    if choice == "1":
        buyer.buy_current_item()
    elif choice == "2":
        keyword = input("请输入搜索关键词: ").strip()
        if keyword:
            buyer.search_and_buy(keyword)
        else:
            print("关键词不能为空")
    elif choice == "3":
        if not whitelist:
            print("❌ 白名单为空，请先确保 config/whitelist_zh.json 存在")
            return
        
        # 按资产分级排序显示
        print("\n" + "=" * 60)
        print("白名单饰品列表 (按年化收益率排序)")
        print("=" * 60)
        
        for i, item in enumerate(whitelist):
            name = item.get("饰品名称", "未知")
            price = item.get("推荐求购价", "?")
            sell_price = item.get("悠悠有品售价", "?")
            grade = item.get("资产分级", "?")
            yield_rate = item.get("年化收益率", "?")
            # 截断过长的名称
            display_name = name if len(name) <= 30 else name[:27] + "..."
            print(f"  {i+1:>2}. [{grade}] {display_name}  求购:{price} 售:{sell_price} 年化:{yield_rate}")
        
        print(f"\n共 {len(whitelist)} 件，输入编号选择 (1-{len(whitelist)})，输入 'all' 批量购买")
        selection = input("请选择: ").strip()
        
        if selection.lower() == "all":
            print("\n⚠️ 批量购买模式 - 将依次搜索并购买所有白名单饰品")
            confirm = input("确认开始? (y/n): ").strip().lower()
            if confirm == "y":
                for i, item in enumerate(whitelist):
                    name = item["饰品名称"]
                    price = item.get("推荐求购价", "未知")
                    print(f"\n{'='*50}")
                    print(f"[{i+1}/{len(whitelist)}] {name} (推荐求购价: {price})")
                    print(f"{'='*50}")
                    buyer.search_and_buy(name)
                    # 购买后回到首页
                    buyer.go_back()
                    time.sleep(1)
            else:
                print("已取消")
        elif selection.isdigit():
            idx = int(selection) - 1
            if 0 <= idx < len(whitelist):
                item = whitelist[idx]
                name = item["饰品名称"]
                price = item.get("推荐求购价", "未知")
                print(f"\n已选择: {name} (推荐求购价: {price})")
                buyer.search_and_buy(name)
            else:
                print("无效编号")
        else:
            print("无效输入")
    elif choice == "4":
        buyer.controller.screenshot()
    else:
        print("无效选择")


if __name__ == "__main__":
    main()
