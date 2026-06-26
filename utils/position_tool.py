# -*- coding: utf-8 -*-
"""
坐标抓取工具 (Position Tool)

基于 tkinter 实现的轻量级坐标抓取工具，用于获取 MuMu 模拟器或其他 ADB 设备的屏幕坐标。
参考自: https://github.com/jaySONG520/wzry_ai/blob/main/showposition.py

功能：
1. 连接 ADB 设备 (MuMu 模拟器)
2. 获取屏幕截图并显示
3. 点击图片获取坐标 (X, Y) 和百分比位置
4. 支持缩放显示以适应不同屏幕

使用方法：
直接运行此脚本： python utils/position_tool.py
"""

import os
import sys
import subprocess
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 尝试添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

class ADBController:
    """简化的ADB控制器"""
    
    # MuMu ADB常见路径
    MUMU_ADB_PATHS = [
        r"C:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe",  # MuMu 12 新版
        r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
        r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"D:\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Netease\MuMuPlayer-12.0\shell\adb.exe",
        r"C:\Program Files\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
        r"D:\Program Files\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
    ]

    def __init__(self, port=16384):
        self.device = f"127.0.0.1:{port}"
        self.adb_path = self._find_adb()
        self.connected = False
        
    def _find_adb(self):
        """查找ADB路径"""
        for path in self.MUMU_ADB_PATHS:
            if os.path.exists(path):
                print(f"找到MuMu ADB: {path}")
                return path
        
        # 尝试系统PATH
        try:
            result = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return "adb"
        except:
            pass
        return None

    def _run_adb(self, args, timeout=10):
        if not self.adb_path:
            return None
        cmd = [self.adb_path] + args
        try:
            # 使用utf-8并忽略错误
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                timeout=timeout,
                encoding='utf-8',
                errors='ignore'
            )
            return result.stdout.strip() if result.stdout else ""
        except Exception as e:
            print(f"ADB Error: {e}")
            return None

    def connect(self):
        """连接设备"""
        print(f"连接设备: {self.device}")
        res = self._run_adb(["connect", self.device])
        if res and ("connected" in res or "already connected" in res):
            self.connected = True
            return True, res
        return False, res

    def screencap(self, local_path):
        """截屏并保存到本地"""
        if not self.connected:
            return False
        
        # 使用 exec-out screencap -p 直接输出png流到文件
        # 注意：某些旧版adb可能不支持exec-out，这里使用最通用的方式：先存手机再拉取
        try:
            # Android截图保存到临时文件
            self._run_adb(["-s", self.device, "shell", "screencap", "-p", "/data/local/tmp/temp_screenshot.png"])
            # 拉取到本地
            self._run_adb(["-s", self.device, "pull", "/data/local/tmp/temp_screenshot.png", local_path])
            # 删除临时文件
            self._run_adb(["-s", self.device, "shell", "rm", "/data/local/tmp/temp_screenshot.png"])
            return True
        except Exception as e:
            print(f"Screenshot error: {e}")
            return False

class PositionTool(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MuMu坐标抓取工具")
        # 缩小初始窗口大小
        self.geometry("1100x700")
        
        self.adb = ADBController(port=16384) # 默认端口
        self.image_path = "screenshot.png"
        self.photo = None
        self.raw_photo = None
        self.pil_image = None
        self.original_width = 0
        self.original_height = 0
        self.display_scale = 1.0 # 显示缩放比例
        self.auto_fit = True # 默认开启自适应
        
        # 标记点数据 [(real_x, real_y, number)]
        self.markers = [] 
        
        # 尝试导入Pillow
        try:
            from PIL import Image, ImageTk, ImageGrab
            self.HAS_PILLOW = True
        except ImportError:
            self.HAS_PILLOW = False
            print("Warning: Pillow not installed, quality will be lower.")
            
        self._init_ui()
        
        # 启动后自动连接
        self.after(500, self.auto_connect)
        
    def _init_ui(self):
        # 左侧控制面板
        control_frame = ttk.Frame(self, padding="10")
        control_frame.pack(side=tk.LEFT, fill=tk.Y)
        
        # 端口设置
        ttk.Label(control_frame, text="MuMu 端口:").pack(anchor=tk.W, pady=(0, 5))
        self.port_entry = ttk.Entry(control_frame)
        self.port_entry.insert(0, "16384")
        self.port_entry.pack(fill=tk.X, pady=(0, 10))
        
        # 连接按钮
        self.connect_btn = ttk.Button(control_frame, text="连接 MuMu", command=self.connect_device)
        self.connect_btn.pack(fill=tk.X, pady=(0, 10))
        
        # 截图按钮
        self.cap_btn = ttk.Button(control_frame, text="截取屏幕 (F5)", command=self.capture_screen)
        self.cap_btn.pack(fill=tk.X, pady=(0, 10))
        
        # 打开/粘贴按钮区域
        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.open_btn = ttk.Button(btn_frame, text="打开图片", command=self.open_image)
        self.open_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        
        self.paste_btn = ttk.Button(btn_frame, text="粘贴 (Ctrl+V)", command=self.paste_image)
        self.paste_btn.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(2, 0))

        # 缩放控制 - 自适应开关
        self.auto_fit_var = tk.BooleanVar(value=True)
        self.auto_fit_chk = ttk.Checkbutton(control_frame, text="自适应窗口大小 (Auto Fit)", variable=self.auto_fit_var, command=self.toggle_auto_fit)
        self.auto_fit_chk.pack(anchor=tk.W, pady=(10, 5))

        # 缩放控制 - 滑块 (当Auto Fit关闭时启用)
        ttk.Label(control_frame, text="手动缩放:").pack(anchor=tk.W, pady=(5, 5))
        
        scale_frame = ttk.Frame(control_frame)
        scale_frame.pack(fill=tk.X)
        
        self.scale_var = tk.DoubleVar(value=0.5) 
        self.scale_scale = ttk.Scale(scale_frame, from_=0.1, to=1.5, variable=self.scale_var, command=self.update_scale_slider)
        self.scale_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 初始禁用滑块
        self.scale_scale.state(['disabled'])
        
        self.scale_label = ttk.Label(scale_frame, text="Auto", width=4)
        self.scale_label.pack(side=tk.RIGHT, padx=5)

        # 常用缩放按钮
        quick_scale_frame = ttk.Frame(control_frame)
        quick_scale_frame.pack(fill=tk.X, pady=5)
        ttk.Button(quick_scale_frame, text="100%", width=5, command=lambda: self.set_manual_scale(1.0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_scale_frame, text="75%", width=5, command=lambda: self.set_manual_scale(0.75)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_scale_frame, text="50%", width=5, command=lambda: self.set_manual_scale(0.5)).pack(side=tk.LEFT, padx=2)

        # 坐标显示区域
        ttk.Label(control_frame, text="坐标记录:").pack(anchor=tk.W, pady=(20, 5))
        self.log_text = tk.Text(control_frame, width=30, height=15)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 复制与清空按钮 - 第一行
        action_frame1 = ttk.Frame(control_frame)
        action_frame1.pack(fill=tk.X, pady=(5, 2))
        
        ttk.Button(action_frame1, text="复制最近一条", command=self.copy_last_coord).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        ttk.Button(action_frame1, text="复制全部", command=self.copy_all_coords).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(2, 0))
        
        # 第二行
        action_frame2 = ttk.Frame(control_frame)
        action_frame2.pack(fill=tk.X, pady=(2, 5))
        
        ttk.Button(action_frame2, text="复制图片", command=self.copy_image_to_clipboard).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        ttk.Button(action_frame2, text="清空所有 (Del)", command=self.clear_all).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(2, 0))
        
        # 右侧图片显示区域
        self.canvas_frame = ttk.Frame(self)
        self.canvas_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        self.canvas = tk.Canvas(self.canvas_frame, bg="#333333", cursor="cross")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        # 监听窗口大小变化
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        
        # 绑定快捷键
        self.bind("<F5>", lambda e: self.capture_screen())
        self.bind("<Control-v>", lambda e: self.paste_image())
        self.bind("<Control-c>", lambda e: self.copy_last_coord())
        self.bind("<Control-Shift-C>", lambda e: self.copy_all_coords())
        self.bind("<Control-Shift-I>", lambda e: self.copy_image_to_clipboard())
        self.bind("<Delete>", lambda e: self.clear_all())
        
        # 状态栏
        status_msg = "就绪 - 点击图片标记坐标，标记点会自动吸附图片"
        self.status_var = tk.StringVar(value=status_msg)
        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.BOTTOM, fill=tk.X)

    def log(self, msg):
        """记录日志"""
        print(msg)
        self.status_var.set(msg)
        
    def clear_all(self):
        """清空日志和标记"""
        self.log_text.delete(1.0, tk.END)
        self.markers.clear()
        self.update_image_display()
        self.log("已清空所有记录和标记")

    def copy_last_coord(self):
        """复制最后一条坐标记录"""
        try:
            content = self.log_text.get(1.0, tk.END).strip()
            if not content:
                self.log("没有可复制的记录")
                return
            
            lines = content.split('\n')
            last_line = lines[-1]
            # 提取坐标 (x, y)
            import re
            match = re.search(r'\((\d+), (\d+)\)', last_line)
            if match:
                coord_text = f"{match.group(1)}, {match.group(2)}"
                self.clipboard_clear()
                self.clipboard_append(coord_text)
                self.log(f"已复制: {coord_text}")
            else:
                self.clipboard_clear()
                self.clipboard_append(last_line)
                self.log("已复制最后一行记录")
        except Exception as e:
            self.log(f"复制失败: {e}")

    def copy_all_coords(self):
        """复制所有坐标记录"""
        try:
            content = self.log_text.get(1.0, tk.END).strip()
            if not content:
                self.log("没有可复制的记录")
                return
            
            self.clipboard_clear()
            self.clipboard_append(content)
            line_count = len(content.split('\n'))
            self.log(f"已复制全部 {line_count} 条记录")
        except Exception as e:
            self.log(f"复制失败: {e}")

    def copy_image_to_clipboard(self):
        """复制当前图片到系统剪贴板 (修复版)"""
        if not self.HAS_PILLOW:
            self.log("需要安装 Pillow 才能复制图片")
            return
        
        if not self.pil_image:
            self.log("没有可复制的图片")
            return
        
        try:
            import io
            from PIL import Image
            import ctypes
            from ctypes import wintypes
            
            # 使用 DIB 格式 (Device Independent Bitmap)
            output = io.BytesIO()
            self.pil_image.convert("RGB").save(output, "BMP")
            data = output.getvalue()[14:]  # 去掉BMP文件头(14字节)
            output.close()
            
            # Windows API 定义
            OpenClipboard = ctypes.windll.user32.OpenClipboard
            EmptyClipboard = ctypes.windll.user32.EmptyClipboard
            GetClipboardData = ctypes.windll.user32.GetClipboardData
            SetClipboardData = ctypes.windll.user32.SetClipboardData
            CloseClipboard = ctypes.windll.user32.CloseClipboard
            
            GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
            GlobalLock = ctypes.windll.kernel32.GlobalLock
            GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
            
            GMEM_MOVEABLE = 0x0002
            CF_DIB = 8
            
            OpenClipboard(0)
            try:
                EmptyClipboard()
                
                # 分配全局内存
                hGlobal = GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not hGlobal:
                    raise Exception("GlobalAlloc failed")
                    
                # 锁定内存获取指针
                lpGlobal = GlobalLock(hGlobal)
                if not lpGlobal:
                    raise Exception("GlobalLock failed")
                
                # 复制数据
                ctypes.memmove(lpGlobal, data, len(data))
                
                # 解锁内存
                GlobalUnlock(hGlobal)
                
                # 设置剪贴板数据
                if not SetClipboardData(CF_DIB, hGlobal):
                    raise Exception("SetClipboardData failed")
                    
            finally:
                CloseClipboard()
            
            w, h = self.pil_image.size
            self.log(f"已复制图片到剪贴板 ({w}x{h})")
        except Exception as e:
            self.log(f"复制图片失败: {e}")
            import traceback
            traceback.print_exc()

    def auto_connect(self):
        """自动连接"""
        self.log("启动自动连接...")
        self.connect_device()

    def connect_device(self):
        port_str = self.port_entry.get().strip()
        if not port_str.isdigit():
            messagebox.showerror("错误", "端口必须是数字")
            return
            
        self.adb = ADBController(port=int(port_str))
        self.log(f"正在连接到端口 {port_str}...")
        self.update()
        
        success, msg = self.adb.connect()
        if success:
            self.log(f"连接成功: {msg}")
            # 连接成功后尝试自动截图
            self.capture_screen()
        else:
            self.log(f"连接失败: {msg}")

    def capture_screen(self):
        if not self.adb.connected:
            # 尝试自动连接
            port_str = self.port_entry.get().strip()
            if port_str.isdigit():
                 self.adb = ADBController(port=int(port_str))
                 self.adb.connect()

        if not self.adb.connected:
             self.log("未连接设备，无法截图")
             return

        self.log("正在截图...")
        self.update()
        
        # 使用时间戳文件名
        timestamp = int(time.time())
        local_path = f"screenshot_{timestamp}.png"
        
        if self.adb.screencap(local_path):
            self.log("截图成功")
            # 截图后清空之前的标记，因为画面变了
            self.markers.clear()
            self.load_image(local_path)
            # 清理旧截图
            if self.image_path and os.path.exists(self.image_path) and self.image_path != local_path:
                 try: os.remove(self.image_path)
                 except: pass
            self.image_path = local_path
        else:
            self.log("截图失败")
            messagebox.showerror("错误", "截图失败")

    def open_image(self):
        filename = filedialog.askopenfilename(filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")])
        if filename:
            self.markers.clear() # 打开新图片清空标记
            self.load_image(filename)
            
    def paste_image(self):
        """从剪贴板粘贴图片"""
        if not self.HAS_PILLOW:
            messagebox.showinfo("提示", "需要安装 Pillow 库才能使用粘贴功能")
            return
            
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img:
                self.markers.clear() # 粘贴新图片清空标记
                # 直接转换Pillow Image对象处理
                self.load_pil_image(img)
                self.log("已从剪贴板粘贴图片")
            else:
                self.log("剪贴板中没有图片")
        except Exception as e:
            self.log(f"粘贴失败: {e}")

    def load_image(self, path):
        try:
            self.original_image_path = path 
            
            if self.HAS_PILLOW:
                from PIL import Image
                pil_image = Image.open(path)
                self.load_pil_image(pil_image)
            else:
                # 降级方案
                self.raw_photo = tk.PhotoImage(file=path)
                self.original_width = self.raw_photo.width()
                self.original_height = self.raw_photo.height()
                self.pil_image = None
                
            self.log(f"加载图片: {os.path.basename(path)} ({self.original_width}x{self.original_height})")
            # 自动调整缩放
            self.fit_window()
            self.update_image_display()
            
        except Exception as e:
            self.log(f"加载图片失败: {e}")
            messagebox.showerror("错误", f"无法加载图片: {e}")

    def load_pil_image(self, pil_image):
        """加载PIL图像对象"""
        self.pil_image = pil_image
        self.original_width, self.original_height = pil_image.size
        self.raw_photo = None # 不再使用Tk PhotoImage作为源

    def toggle_auto_fit(self):
        if self.auto_fit_var.get():
            self.scale_scale.state(['disabled'])
            self.scale_label.config(text="Auto")
            self.update_image_display()
        else:
            self.scale_scale.state(['!disabled'])
            self.scale_label.config(text=f"{int(self.scale_var.get()*100)}%")
            self.update_image_display()

    def set_manual_scale(self, scale):
        self.auto_fit_var.set(False)
        self.toggle_auto_fit()
        self.scale_var.set(scale)
        self.scale_label.config(text=f"{int(scale*100)}%")
        self.update_image_display()

    def update_scale_slider(self, val):
        if not self.auto_fit_var.get():
             self.scale_label.config(text=f"{int(float(val)*100)}%")
             self.update_image_display()

    def fit_window(self):
        """自动调整缩放比例以适应当前Canvas大小"""
        if not (self.pil_image or self.raw_photo): return
        
        # 延迟一下等待UI布局完成
        self.update_idletasks()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        
        if cw > 10 and ch > 10:
             w_scale = cw / self.original_width
             h_scale = ch / self.original_height
             scale = min(w_scale, h_scale, 1.0) # 最多100%
             # 转换为合理的档位
             if scale > 0.9: scale = 1.0
             elif scale > 0.7: scale = 0.75
             elif scale > 0.45: scale = 0.5
             
             self.scale_var.set(scale)
             self.scale_label.config(text=f"{int(scale*100)}%")
             self.display_scale = scale
        
    def on_canvas_resize(self, event):
        """窗口大小改变时触发"""
        if self.auto_fit_var.get() and (self.pil_image or self.raw_photo):
            # 延迟执行避免过于频繁
            if hasattr(self, '_resize_job'):
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(50, self.update_image_display)

    def calculate_auto_fit_scale(self):
        """计算自适应缩放比例"""
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        
        if cw < 10 or ch < 10 or self.original_width == 0:
            return 1.0
            
        w_scale = cw / self.original_width
        h_scale = ch / self.original_height
        
        scale = min(w_scale, h_scale)
        # 限制最大放大倍数
        if scale > 1.5: scale = 1.5
        
        return scale

    def update_image_display(self):
        if not (self.pil_image or self.raw_photo):
            return
            
        if self.auto_fit_var.get():
            target_scale = self.calculate_auto_fit_scale()
            self.display_scale = target_scale
        else:
            target_scale = self.scale_var.get()
            self.display_scale = target_scale

        try:
            if self.HAS_PILLOW and self.pil_image:
                # 使用Pillow的高质量缩放
                from PIL import Image, ImageTk
                
                new_width = int(self.original_width * target_scale)
                new_height = int(self.original_height * target_scale)
                
                # 避免尺寸为0
                new_width = max(1, new_width)
                new_height = max(1, new_height)
                
                # 使用LANCZOS重采样获得最佳质量
                resized_pil = self.pil_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(resized_pil)
                self.current_scale_factor = target_scale
                
            else:
                # 降级到Tkinter原生缩放 (质量较差)
                from fractions import Fraction
                frac = Fraction(target_scale).limit_denominator(20)
                P, Q = frac.numerator, frac.denominator
                
                if P > 10: 
                     if target_scale < 1.0:
                         display_img = self.raw_photo.subsample(int(1/target_scale), int(1/target_scale))
                         self.current_scale_factor = 1.0 / int(1/target_scale)
                     else:
                         display_img = self.raw_photo
                         self.current_scale_factor = 1.0
                else:
                    display_img = self.raw_photo.zoom(P, P).subsample(Q, Q)
                    self.current_scale_factor = P / Q
                     
                self.photo = display_img
            
            # 更新Canvas
            self.canvas.delete("all")
            # 居中绘制
            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()
            img_width = self.photo.width()
            img_height = self.photo.height()
            
            pos_x = max(0, (canvas_width - img_width) // 2)
            pos_y = max(0, (canvas_height - img_height) // 2)
            
            self.img_x = pos_x
            self.img_y = pos_y
            
            self.canvas.create_image(pos_x, pos_y, anchor=tk.NW, image=self.photo)
            
            # 重新绘制所有标记
            self.draw_all_markers()
            
        except Exception as e:
            print(f"Scale error: {e}")

    def draw_all_markers(self):
        """绘制所有已保存的标记"""
        for i, (real_x, real_y, num) in enumerate(self.markers):
            self.draw_marker(real_x, real_y, num)

    def draw_marker(self, real_x, real_y, number):
        """在画布上绘制单个标记"""
        # 计算当前画布坐标
        canvas_x = real_x * self.current_scale_factor + self.img_x
        canvas_y = real_y * self.current_scale_factor + self.img_y
        
        r = 8
        # 绘制红圈 (filled red circle)
        self.canvas.create_oval(canvas_x-r, canvas_y-r, canvas_x+r, canvas_y+r, 
                                fill="red", outline="white", width=1)
        # 绘制数字
        self.canvas.create_text(canvas_x, canvas_y, text=str(number), fill="white", font=("Arial", 9, "bold"))

    def on_canvas_click(self, event):
        if not hasattr(self, 'photo'):
            return
            
        # 获取Canvas上的绝对坐标
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        
        rel_x = canvas_x - self.img_x
        rel_y = canvas_y - self.img_y
        
        if rel_x < 0 or rel_y < 0 or rel_x > self.photo.width() or rel_y > self.photo.height():
            return
            
        # 映射回原图坐标
        real_x = int(rel_x / self.current_scale_factor)
        real_y = int(rel_y / self.current_scale_factor)
        
        # 确保不越界
        real_x = max(0, min(real_x, self.original_width - 1))
        real_y = max(0, min(real_y, self.original_height - 1))
        
        # 计算百分比
        pct_x = real_x / self.original_width
        pct_y = real_y / self.original_height
        
        # 添加新标记
        next_num = len(self.markers) + 1
        self.markers.append((real_x, real_y, next_num))
        self.draw_marker(real_x, real_y, next_num)
        
        # 格式化输出
        log_msg = f"#{next_num} ({real_x}, {real_y})  {pct_x:.3f}, {pct_y:.3f}\n"
        self.log_text.insert(tk.END, log_msg)
        self.log_text.see(tk.END)
        
        print(f"Clicked: {log_msg.strip()}")

if __name__ == "__main__":
    app = PositionTool()
    app.mainloop()
