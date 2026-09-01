import ctypes
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import io
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import pystray
from PIL import Image, ImageDraw, ImageTk
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES, TkinterDnD = None, None


APP_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
FFMPEG = RESOURCE_DIR / "ffmpeg.exe"
APP_ICON = RESOURCE_DIR / "app-logo.ico"
CONFIG = DATA_DIR / "挂播工具.config.json"
MUTEX_NAME = "Global\\MultiPlatformPusher"
BUILTIN_PLATFORMS = {"虎牙直播", "斗鱼直播", "哔哩哔哩直播"}


class PlatformSession:
    def __init__(self, name, data):
        self.name = name
        self.url = data.get("url", "")
        self.video = data.get("video", "")
        self.enabled = bool(data.get("enabled", True))
        self.accounts = data.get("accounts", []) if isinstance(data.get("accounts", []), list) else []
        if not self.accounts and (self.url or self.video):
            self.accounts = [{"name": "默认账号", "url": self.url, "video": self.video, "enabled": self.enabled}]
        self.status = "已停止"
        self.restarts = 0
        self.last_error = ""
        self.process = None
        self.stop_event = threading.Event()
        self.thread = None
        self.account_runtime = {}

    def as_dict(self):
        return {"url": self.url, "video": self.video, "enabled": self.enabled, "accounts": self.accounts}


class MultiPusherApp:
    def __init__(self, root):
        self.root = root
        self.closing = False
        self.poll_after_id = None
        self.already_running = False
        self.root.title("挂播推流工具")
        if APP_ICON.exists():
            try:
                self.root.iconbitmap(str(APP_ICON))
            except tk.TclError:
                pass
        self.root.geometry("1060x700")
        self.root.minsize(900, 600)
        self.events = queue.Queue()
        self.media_cache = {}
        self.media_pending = set()
        self.thumb_cache = {}
        self.thumb_pending = set()
        self.sessions = {}
        self.current = None
        self.tray = None
        self.mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.GetLastError() == 183:
            self.already_running = True
            messagebox.showerror("程序已运行", "挂播推流工具已经启动。")
            root.destroy()
            return
        self.name_var = tk.StringVar()
        self.url_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar(value=True)
        self.retry_var = tk.IntVar(value=10)
        self.auto_retry_var = tk.BooleanVar(value=True)
        self.startup_var = tk.BooleanVar(value=False)
        self.interval_var = tk.IntVar(value=0)
        self.max_concurrent_var = tk.IntVar(value=0)
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="未选择平台")
        self.total_var = tk.StringVar(value="0")
        self.live_var = tk.StringVar(value="0")
        self.dropped_var = tk.StringVar(value="0")
        self.load_config()
        self.build_ui()
        self.refresh_platforms()
        self.poll_after_id = self.root.after(150, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.safe_show_exit_dialog)

    def load_config(self):
        data = {}
        if CONFIG.exists():
            try:
                data = json.loads(CONFIG.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self.retry_var.set(int(data.get("retry", 10)))
        self.auto_retry_var.set(bool(data.get("auto_retry", True)))
        self.startup_var.set(bool(data.get("startup", False)))
        self.interval_var.set(max(0, int(data.get("interval", 0))))
        self.max_concurrent_var.set(max(0, int(data.get("max_concurrent", 0))))
        profiles = data.get("profiles", {})
        if isinstance(profiles, dict):
            self.sessions = {name: PlatformSession(name, value if isinstance(value, dict) else {}) for name, value in profiles.items()}
        for name in BUILTIN_PLATFORMS:
            session = self.sessions.get(name)
            if session and session.accounts:
                for account in session.accounts:
                    if not account.get("url") and Path(account.get("video", "")).name.lower() == "1.mp4":
                        account["video"] = ""
        if not self.sessions:
            self.sessions = {
                "虎牙直播": PlatformSession("虎牙直播", {}),
                "斗鱼直播": PlatformSession("斗鱼直播", {}),
                "哔哩哔哩直播": PlatformSession("哔哩哔哩直播", {}),
            }

    def save_config(self):
        data = {"retry": int(self.retry_var.get()), "auto_retry": self.auto_retry_var.get(), "startup": self.startup_var.get(), "interval": int(self.interval_var.get()), "max_concurrent": int(self.max_concurrent_var.get()), "profiles": {name: s.as_dict() for name, s in self.sessions.items()}}
        try:
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def toggle_startup(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if self.startup_var.get():
                    exe = Path(sys.executable).resolve()
                    command = f'"{exe}"' if getattr(sys, "frozen", False) else f'"{exe}" "{Path(__file__).resolve()}"'
                    winreg.SetValueEx(key, "HuBoPusher", 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, "HuBoPusher")
                    except FileNotFoundError:
                        pass
            self.save_config()
        except OSError as exc:
            self.startup_var.set(False)
            messagebox.showerror("开机启动设置失败", str(exc))

    def media_info(self, video):
        if not video or not Path(video).exists():
            return "媒体信息不可用"
        video = str(Path(video).resolve())
        if video in self.media_cache:
            return self.media_cache[video]
        if video not in self.media_pending:
            self.media_pending.add(video)
            threading.Thread(target=self._load_media_info, args=(video,), daemon=True).start()
        return "正在读取媒体信息..."

    def thumbnail(self, video):
        if not video or not Path(video).exists():
            return None
        video = str(Path(video).resolve())
        if video in self.thumb_cache:
            return self.thumb_cache[video]
        if video not in self.thumb_pending:
            self.thumb_pending.add(video)
            threading.Thread(target=self._load_thumbnail, args=(video,), daemon=True).start()
        return None

    def _load_thumbnail(self, video):
        try:
            result = subprocess.run([str(FFMPEG), "-y", "-ss", "00:00:01", "-i", video, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"], capture_output=True, timeout=12)
            image = Image.open(io.BytesIO(result.stdout)).convert("RGB") if result.stdout else None
            if image:
                image.thumbnail((150, 85))
            self.events.put(("thumb", video, image))
        except (OSError, subprocess.SubprocessError, ValueError):
            self.events.put(("thumb", video, None))
    def _load_media_info(self, video):
        try:
            result = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(video)], capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=8)
            output = result.stderr
            import re
            duration = (re.search(r"Duration:\s*([0-9:.]+)", output) or [None, ""])[1]
            match = re.search(r"Video:\s*([^,\s]+).*?\s(\d{2,5}x\d{2,5})", output, re.S)
            codec, resolution = (match.group(1), match.group(2)) if match else ("", "")
            value = " | ".join(p for p in (duration, resolution, codec) if p) or "媒体信息不可用"
        except (OSError, subprocess.SubprocessError):
            value = "媒体信息不可用"
        self.events.put(("media", video, value))

    def build_ui(self):
        bg, side, border, blue = "#ffffff", "#f5f7fa", "#e5e9f0", "#397cf6"
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground="#243247", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=bg, foreground="#8b98aa", font=("Microsoft YaHei UI", 9))
        style.configure("Card.TLabelframe", background=bg, bordercolor=border, borderwidth=1)
        style.configure("Card.TLabelframe.Label", background=bg, foreground="#526174", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TEntry", fieldbackground="#fff", foreground="#243247", bordercolor="#d5dce5", padding=6)
        style.configure("TCombobox", fieldbackground="#fff", foreground="#243247", background="#fff")
        style.configure("TButton", background="#eef2f7", foreground="#334155", padding=(11, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#e1e8f2")])
        style.configure("Primary.TButton", background=blue, foreground="white", padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#2563eb")])
        style.configure("Stop.TButton", background="#fff1f2", foreground="#c2414d", padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        self.root.configure(bg=bg)
        shell = tk.Frame(self.root, bg=bg)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        nav = tk.Frame(shell, bg=side, width=240)
        nav.grid(row=0, column=0, sticky="nsew")
        nav.grid_propagate(False)
        tk.Label(nav, text="◈  挂播推流工具", bg=side, fg="#1f2d3d", font=("Microsoft YaHei UI", 15, "bold"), anchor="w").pack(fill="x", padx=22, pady=(28, 4))
        tk.Label(nav, text="多平台直播控制台", bg=side, fg="#9aa6b5", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", padx=24, pady=(0, 24))
        tk.Label(nav, text="工作台", bg=side, fg="#738196", font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill="x", padx=24, pady=(0, 8))
        self.overview_btn = tk.Label(nav, text="▦   总览", bg="#e2e8f0", fg="#243247", font=("Microsoft YaHei UI", 10, "bold"), anchor="w", padx=18, pady=11, cursor="hand2")
        self.overview_btn.pack(fill="x", padx=10)
        self.overview_btn.bind("<Button-1>", lambda _event: self.show_overview())
        tk.Label(nav, text="平台列表", bg=side, fg="#738196", font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill="x", padx=24, pady=(25, 8))
        self.platform_list = tk.Frame(nav, bg=side)
        self.platform_list.pack(fill="both", expand=True, padx=10)
        self.platform_buttons = {}
        tk.Button(nav, text="＋ 更多平台 / 新建", command=self.safe_add_platform, bg=blue, fg="white", bd=0, relief="flat", font=("Microsoft YaHei UI", 10, "bold"), pady=8).pack(fill="x", padx=14, pady=12)
        tk.Label(nav, text="版本 1.0.0", bg=side, fg="#a1acba", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", padx=24, pady=(0, 2))
        tk.Label(nav, text="交流群 1101062939", bg=side, fg="#a1acba", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", padx=24, pady=(0, 18))
        ttk.Button(nav, text="关于 / 免责声明", command=self.show_about).pack(fill="x", padx=14, pady=(0, 10))

        self.content = tk.Frame(shell, bg=bg)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)
        header = tk.Frame(self.content, bg=bg, height=82)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(header, text="▦  总览", bg=bg, fg="#1f2d3d", font=("Microsoft YaHei UI", 21, "bold"), anchor="w").pack(side="left", padx=30)
        self.header_hint = tk.Label(header, text="0 个平台配置", bg=bg, fg="#9aa6b5", font=("Microsoft YaHei UI", 9))
        self.header_hint.pack(side="right", padx=30)
        ttk.Checkbutton(header, text="开机启动", variable=self.startup_var, command=self.toggle_startup).pack(side="right", padx=(0, 18))
        ttk.Button(header, text="设置", command=self.show_settings).pack(side="right", padx=(0, 12))
        ttk.Separator(self.content).grid(row=0, column=0, sticky="se")
        self.show_overview()

    def clear_content(self):
        for child in self.content.grid_slaves(row=1):
            child.destroy()

    def show_overview(self):
        self.current = None
        self.clear_content()
        body = ttk.Frame(self.content, padding=(30, 24))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        ttk.Label(body, text="直播总览", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="查看所有平台的实时运行状态", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 18))
        cards = tk.Frame(body, bg="#ffffff")
        cards.grid(row=2, column=0, sticky="ew", pady=(0, 20))
        for i in range(3): cards.columnconfigure(i, weight=1)
        self.stat_card(cards, 0, "平台总数", self.total_var, "#397cf6")
        self.stat_card(cards, 1, "正在直播", self.live_var, "#16a34a")
        self.stat_card(cards, 2, "已掉线", self.dropped_var, "#ef4444")
        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, sticky="w", pady=(0, 12))
        ttk.Button(actions, text="▶  批量开始全部平台", command=self.start_all, style="Primary.TButton").pack(side=tk.LEFT)
        ttk.Button(actions, text="■  停止全部平台", command=self.stop_all, style="Stop.TButton").pack(side=tk.LEFT, padx=8)
        status_header = ttk.Frame(body)
        status_header.grid(row=4, column=0, sticky="ew")
        ttk.Label(status_header, text="平台运行状态", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        filter_row = ttk.Frame(status_header)
        filter_row.pack(side="right")
        ttk.Label(filter_row, text="筛选").pack(side="left", padx=(0, 6))
        filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var, width=24)
        filter_entry.pack(side="left")
        self.filter_var.trace_add("write", lambda *_: self.update_overview())
        table_frame = ttk.Frame(body)
        table_frame.grid(row=5, column=0, sticky="nsew", pady=(7, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.overview_tree = ttk.Treeview(table_frame, columns=("platform", "account", "status", "restarts", "video"), show="headings", selectmode="browse")
        headings = (("platform", "平台", 150), ("account", "账号", 170), ("status", "状态", 110), ("restarts", "重启次数", 90), ("video", "视频文件", 360))
        for key, title, width in headings:
            self.overview_tree.heading(key, text=title)
            self.overview_tree.column(key, width=width, minwidth=70, anchor="w")
        self.overview_tree.grid(row=0, column=0, sticky="nsew")
        overview_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.overview_tree.yview)
        overview_scroll.grid(row=0, column=1, sticky="ns")
        self.overview_tree.configure(yscrollcommand=overview_scroll.set)
        self.overview_tree.tag_configure("live", foreground="#15803d")
        self.overview_tree.tag_configure("error", foreground="#dc2626")
        self.overview_tree.tag_configure("waiting", foreground="#b45309")
        self.update_overview()

    def stat_card(self, parent, col, label, variable, color):
        card = tk.Frame(parent, bg="#ffffff", highlightbackground="#e5e9f0", highlightthickness=1, height=92)
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 8 if col < 2 else 0))
        card.grid_propagate(False)
        tk.Frame(card, bg=color, width=4).pack(side="left", fill="y")
        tk.Label(card, text=label, bg="#ffffff", fg="#8b98aa", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=18, pady=(17, 0))
        tk.Label(card, textvariable=variable, bg="#ffffff", fg="#243247", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w", padx=18)

    def refresh_platforms(self):
        for child in self.platform_list.winfo_children(): child.destroy()
        self.platform_buttons = {}
        for name in self.sessions:
            logo = self.load_logo(name)
            button = tk.Button(self.platform_list, text=f"  {name}", image=logo, compound="left", command=lambda n=name: self.select_platform(n), anchor="w", bg="#f5f7fa", fg="#536276", activebackground="#dbeafe", activeforeground="#1d4ed8", bd=0, relief="flat", font=("Microsoft YaHei UI", 10), padx=10, pady=8)
            button.image = logo
            button.pack(fill="x", pady=2)
            self.platform_buttons[name] = button
        self.total_var.set(str(len(self.sessions)))
        self.header_hint.configure(text=f"{len(self.sessions)} 个平台配置")
        self.update_overview()

    def load_logo(self, name):
        filename = {"虎牙直播": "huya-logo.png", "斗鱼直播": "douyu-logo.png", "哔哩哔哩直播": "bilibili-logo.ico"}.get(name)
        path = RESOURCE_DIR / filename if filename else None
        try:
            if path and path.exists():
                image = Image.open(path).convert("RGBA"); image.thumbnail((24, 24)); return ImageTk.PhotoImage(image)
        except Exception:
            pass
        image = Image.new("RGBA", (24, 24), "#dbeafe"); ImageDraw.Draw(image).text((6, 4), name[:1], fill="#2563eb"); return ImageTk.PhotoImage(image)

    def add_platform(self):
        dialog = tk.Toplevel(self.root)
        body = self.dialog_shell(dialog, "新建平台", 500, 250)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="新建平台", bg="white", fg="#172033", font=("Microsoft YaHei UI", 18, "bold"), anchor="w").pack(fill="x", padx=30, pady=(22, 5))
        tk.Label(body, text="添加一个平台配置，用于单独管理推流任务。", bg="white", fg="#718096", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=30, pady=(0, 18))
        name_var = tk.StringVar()
        entry = ttk.Entry(body, textvariable=name_var, font=("Microsoft YaHei UI", 10))
        entry.pack(fill="x", padx=30, ipady=5)
        entry.focus_set()
        buttons = tk.Frame(body, bg="white")
        buttons.pack(fill="x", padx=30, pady=25)
        def create():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("名称无效", "请输入平台名称。", parent=dialog)
                return
            if name in self.sessions:
                messagebox.showerror("添加失败", "平台名称已存在。", parent=dialog)
                return
            self.sessions[name] = PlatformSession(name, {})
            self.save_config(); self.refresh_platforms(); dialog.destroy()
            self.select_platform(name)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="创建平台", command=create, style="Primary.TButton").pack(side="right")
        dialog.bind("<Return>", lambda _event: create())

    def safe_add_platform(self):
        try:
            self.add_platform()
        except Exception as exc:
            messagebox.showerror("新建平台失败", str(exc), parent=self.root)

    def dialog_shell(self, dialog, title, width, height):
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.configure(bg="white")
        dialog.transient(self.root)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grab_set()
        return tk.Frame(dialog, bg="white")

    def select_platform_legacy(self, name=None):
        if not name or name not in self.sessions:
            return
        session = self.sessions[name]
        self.current = name
        self.clear_content()
        body = ttk.Frame(self.content, padding=(30, 24))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text=f"平台：{name}", font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="平台视频和推流任务信息", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 16))
        frame = ttk.LabelFrame(body, text="  平台设置  ", padding=14, style="Card.TLabelframe")
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        frame.columnconfigure(1, weight=1)
        self.name_var.set(name); self.url_var.set(session.url); self.video_var.set(session.video); self.enabled_var.set(session.enabled)
        ttk.Label(frame, text="推流地址").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.url_var, show="*").grid(row=0, column=1, columnspan=2, sticky="ew", padx=10)
        ttk.Label(frame, text="视频文件").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.video_var).grid(row=1, column=1, sticky="ew", padx=10)
        ttk.Button(frame, text="选择文件", command=self.choose_video).grid(row=1, column=2)
        ttk.Checkbutton(frame, text="加入批量任务", variable=self.enabled_var).grid(row=2, column=1, sticky="w", padx=10, pady=5)
        account_frame = ttk.LabelFrame(body, text="  推流账号  ", padding=12, style="Card.TLabelframe")
        account_frame.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        account_frame.columnconfigure(0, weight=1)
        self.account_list = tk.Listbox(account_frame, height=4, bd=0, highlightthickness=1, highlightbackground="#e5e9f0", activestyle="none", font=("Microsoft YaHei UI", 10), bg="#f8fafc", fg="#475569", selectbackground="#dbeafe", selectforeground="#1d4ed8")
        self.account_list.grid(row=0, column=0, rowspan=2, sticky="ew", padx=(0, 12))
        for account in session.accounts:
            self.account_list.insert(tk.END, account.get("name", "未命名账号"))
        ttk.Button(account_frame, text="＋ 添加账号", command=lambda: self.add_account(name)).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(account_frame, text="删除账号", command=lambda: self.delete_account(name)).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(account_frame, text="一个平台可管理多个独立推流账号", style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        actions = ttk.Frame(body); actions.grid(row=4, column=0, sticky="w", pady=(0, 12))
        ttk.Button(actions, text="保存平台", command=self.save_current).pack(side=tk.LEFT)
        ttk.Button(actions, text="▶  批量启动账号", command=lambda: self.start_session(name), style="Primary.TButton").pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="■  批量停止账号", command=lambda: self.stop_session(name), style="Stop.TButton").pack(side=tk.LEFT)
        ttk.Button(actions, text="删除平台", command=self.delete_current).pack(side=tk.LEFT, padx=8)
        status = tk.Frame(body, bg="#f8fafc", highlightbackground="#e5e9f0", highlightthickness=1)
        status.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        self.status_var.set(session.status)
        tk.Label(status, text="运行状态", bg="#f8fafc", fg="#8b98aa", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=16, pady=14)
        tk.Label(status, textvariable=self.status_var, bg="#f8fafc", fg="#16a34a", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Label(status, text=f"重启次数：{session.restarts}    最近错误：{session.last_error or '无'}", bg="#f8fafc", fg="#64748b", font=("Microsoft YaHei UI", 9)).pack(side="right", padx=16)

    def choose_video(self):
        path = filedialog.askopenfilename(title="选择视频文件", filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.flv *.ts"), ("所有文件", "*.*")])
        if path: self.video_var.set(path)

    # Account-first platform view. The legacy platform-level fields remain
    # readable from old config files, but are no longer shown in the UI.
    def select_platform(self, name=None):
        if not name or name not in self.sessions:
            return
        session = self.sessions[name]
        self.current = name
        self.clear_content()
        body = ttk.Frame(self.content, padding=(30, 24))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=f"平台：{name}", font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="＋ 新建账号", command=lambda: self.add_account(name), style="Primary.TButton").grid(row=0, column=1, sticky="e")
        delete_button = ttk.Button(top, text="删除平台", command=self.delete_current)
        delete_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        if name in BUILTIN_PLATFORMS:
            delete_button.configure(state=tk.DISABLED)
        ttk.Label(body, text="平台账号管理", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 16))
        cards = tk.Frame(body, bg="#ffffff")
        cards.grid(row=2, column=0, sticky="new")
        self.account_vars = {}
        for index, account in enumerate(session.accounts):
            self.account_card(cards, name, index, account)
        if not session.accounts:
            tk.Label(cards, text="暂无账号，点击右上角“新建账号”开始配置", bg="#f8fafc", fg="#8b98aa", font=("Microsoft YaHei UI", 11), pady=35).grid(row=0, column=0, columnspan=2, sticky="ew")
        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, sticky="w", pady=(20, 0))
        ttk.Button(actions, text="全选", command=lambda: self.set_all_accounts(name, True)).pack(side=tk.LEFT)
        ttk.Button(actions, text="全不选", command=lambda: self.set_all_accounts(name, False)).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="反选", command=lambda: self.toggle_all_accounts(name)).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(actions, text="▶  批量启动账号", command=lambda: self.start_session(name), style="Primary.TButton").pack(side=tk.LEFT)
        ttk.Button(actions, text="■  批量停止账号", command=lambda: self.stop_session(name), style="Stop.TButton").pack(side=tk.LEFT, padx=8)

    def account_card(self, parent, platform_name, index, account):
        row, col = divmod(index, 2)
        card = tk.Frame(parent, bg="#ffffff", highlightbackground="#e4e9f0", highlightthickness=1, padx=16, pady=13)
        card.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 10, 10 if col == 0 else 0), pady=(0, 10))
        parent.columnconfigure(0, weight=1); parent.columnconfigure(1, weight=1)
        runtime = self.sessions[platform_name].account_runtime.get(account.get("name", ""), {})
        selected = tk.BooleanVar(value=bool(account.get("enabled", True)))
        self.account_vars[(platform_name, index)] = selected
        def update_enabled():
            account["enabled"] = bool(selected.get())
            if not account["enabled"]:
                self.stop_one_account(platform_name, account.get("name", ""))
            self.save_config()
        header = tk.Frame(card, bg="#ffffff"); header.pack(fill="x")
        tk.Checkbutton(header, variable=selected, command=update_enabled, bg="#ffffff", activebackground="#ffffff", selectcolor="#ffffff", bd=0).pack(side="left")
        tk.Label(header, text=f"●  {account.get('name', '未命名账号')}", bg="#ffffff", fg="#243247", font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(side="left")
        tk.Label(card, text=f"状态：{runtime.get('status', '已停止')}    重启：{runtime.get('restarts', 0)}", bg="#ffffff", fg="#16a34a", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(8, 3))
        tk.Label(card, text=f"视频：{Path(account.get('video', '')).name if account.get('video') else '未选择'}", bg="#ffffff", fg="#718096", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x")
        thumb = self.thumbnail(account.get("video", ""))
        if thumb:
            preview = ImageTk.PhotoImage(thumb)
            self._thumb_refs = getattr(self, "_thumb_refs", []) + [preview]
            tk.Label(card, image=preview, bg="#f8fafc", width=150, height=85).pack(anchor="w", pady=(6, 0))
        else:
            tk.Label(card, text="视频首帧读取中..." if account.get("video") else "未选择视频", bg="#f8fafc", fg="#94a3b8", width=24, height=4).pack(anchor="w", pady=(6, 0))
        tk.Label(card, text=f"媒体：{self.media_info(account.get('video', ''))}", bg="#ffffff", fg="#94a3b8", font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x", pady=(2, 0))
        buttons = tk.Frame(card, bg="#ffffff"); buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="编辑", command=lambda i=index: self.edit_account(platform_name, i)).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="删除账号", command=lambda i=index: self.delete_account_by_index(platform_name, i)).pack(side="right")
        ttk.Button(buttons, text="日志", command=lambda: self.show_account_log(platform_name, account.get("name", ""))).pack(side="right", padx=(0, 6))
        if runtime.get("status") in ("直播中", "启动中", "等待重连"):
            ttk.Button(buttons, text="停止", command=lambda: self.stop_one_account(platform_name, account.get("name", ""))).pack(side="left")
        else:
            ttk.Button(buttons, text="启动", command=lambda: self.start_one_account(platform_name, account)).pack(side="left")

    def delete_account_by_index(self, platform_name, index):
        session = self.sessions.get(platform_name)
        if session and 0 <= index < len(session.accounts):
            account_name = session.accounts[index].get("name", "")
            runtime = session.account_runtime.get(account_name)
            if runtime: runtime["stop"].set()
            del session.accounts[index]
            self.save_config(); self.select_platform(platform_name)

    def set_all_accounts(self, platform_name, enabled):
        for index, account in enumerate(self.sessions[platform_name].accounts):
            account["enabled"] = enabled
            var = self.account_vars.get((platform_name, index))
            if var: var.set(enabled)
            if not enabled: self.stop_one_account(platform_name, account.get("name", ""))
        self.save_config()

    def toggle_all_accounts(self, platform_name):
        for index, account in enumerate(self.sessions[platform_name].accounts):
            account["enabled"] = not account.get("enabled", True)
            var = self.account_vars.get((platform_name, index))
            if var: var.set(account["enabled"])
            if not account["enabled"]: self.stop_one_account(platform_name, account.get("name", ""))
        self.save_config()

    def show_account_log(self, platform_name, account_name):
        runtime = self.sessions[platform_name].account_runtime.get(account_name, {})
        dialog = tk.Toplevel(self.root); dialog.title(f"日志 - {platform_name}/{account_name}"); dialog.geometry("720x420"); dialog.transient(self.root)
        text = scrolledtext.ScrolledText(dialog, wrap=tk.WORD, font=("Consolas", 9)); text.pack(fill="both", expand=True, padx=10, pady=10)
        def refresh():
            if not dialog.winfo_exists():
                return
            current = self.sessions.get(platform_name, {}).account_runtime.get(account_name, {}) if platform_name in self.sessions else {}
            text.configure(state=tk.NORMAL); text.delete("1.0", tk.END)
            text.insert("1.0", "\n".join(current.get("logs", [])) or "暂无日志")
            text.see(tk.END); text.configure(state=tk.DISABLED)
            dialog.after(500, refresh)
        refresh()
        buttons = ttk.Frame(dialog); buttons.pack(fill="x", padx=10, pady=(0, 10))
        def copy_log():
            self.root.clipboard_clear(); self.root.clipboard_append(text.get("1.0", tk.END))
        def clear_log():
            runtime = self.sessions.get(platform_name).account_runtime.get(account_name) if platform_name in self.sessions else None
            if runtime: runtime["logs"] = []
            refresh()
        def export_log():
            path = filedialog.asksaveasfilename(title="导出日志", defaultextension=".log", filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt")])
            if path: Path(path).write_text(text.get("1.0", tk.END), encoding="utf-8")
        ttk.Button(buttons, text="复制").configure(command=copy_log)
        ttk.Button(buttons, text="清空").configure(command=clear_log)
        ttk.Button(buttons, text="导出").configure(command=export_log)
        for child in buttons.winfo_children(): child.pack(side="right", padx=(6, 0))

    def show_settings(self):
        dialog = tk.Toplevel(self.root); dialog.title("运行设置"); dialog.geometry("460x300"); dialog.transient(self.root); dialog.grab_set()
        body = self.dialog_shell(dialog, "运行设置", 460, 300); body.pack(fill="both", expand=True)
        for label, var in (("自动重连间隔（秒）", self.retry_var), ("账号启动间隔（秒）", self.interval_var), ("最大并发账号（0 为不限）", self.max_concurrent_var)):
            row = tk.Frame(body, bg="white"); row.pack(fill="x", padx=30, pady=9)
            tk.Label(row, text=label, width=22, anchor="w", bg="white", fg="#526174").pack(side="left")
            ttk.Spinbox(row, from_=0, to=86400, textvariable=var, width=12).pack(side="left")
        ttk.Checkbutton(body, text="自动重连", variable=self.auto_retry_var).pack(anchor="w", padx=30, pady=8)
        ttk.Button(body, text="保存设置", style="Primary.TButton", command=lambda: (self.save_config(), dialog.destroy())).pack(anchor="e", padx=30, pady=18)

    def show_about(self):
        dialog = tk.Toplevel(self.root)
        body = self.dialog_shell(dialog, "关于挂播推流工具", 620, 360)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="挂播推流工具", bg="white", fg="#243247", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=30, pady=(24, 4))
        tk.Label(body, text="版本 1.0.0  |  交流群 1101062939", bg="white", fg="#718096", font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=30, pady=(0, 18))
        disclaimer = ("本工具仅供个人学习、技术研究与软件测试使用。请遵守相关法律法规及各直播平台的用户协议，\n"
                     "不得用于未经授权的推流、刷量、欺诈、侵权或其他违法违规用途。因使用本工具产生的任何后果\n"
                     "由使用者自行承担，开发者不对违规使用行为负责。")
        tk.Label(body, text="免责声明", bg="white", fg="#526174", font=("Microsoft YaHei UI", 10, "bold"), anchor="w").pack(fill="x", padx=30)
        tk.Label(body, text=disclaimer, bg="#f8fafc", fg="#64748b", justify="left", anchor="w", padx=16, pady=14, font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=30, pady=(8, 20))
        ttk.Button(body, text="关闭", command=dialog.destroy).pack(anchor="e", padx=30)

    def edit_account(self, platform_name, index):
        session = self.sessions[platform_name]
        account = session.accounts[index]
        dialog = tk.Toplevel(self.root)
        body = self.dialog_shell(dialog, "编辑推流账号", 560, 330)
        body.pack(fill="both", expand=True)
        values = {"name": tk.StringVar(value=account.get("name", "")), "url": tk.StringVar(value=account.get("url", "")), "video": tk.StringVar(value=account.get("video", ""))}
        for label, key in (("账号名称", "name"), ("推流地址", "url"), ("视频文件", "video")):
            row = tk.Frame(body, bg="white"); row.pack(fill="x", padx=30, pady=7)
            tk.Label(row, text=label, width=10, anchor="w", bg="white", fg="#526174", font=("Microsoft YaHei UI", 10)).pack(side="left")
            entry = ttk.Entry(row, textvariable=values[key], show="*" if key == "url" else "")
            entry.pack(side="left", fill="x", expand=True)
            if key == "video": self.bind_video_drop(entry, values[key])
            if key == "video": ttk.Button(row, text="选择", command=lambda: self.pick_account_video(values["video"])).pack(side="left", padx=(8, 0))
        buttons = tk.Frame(body, bg="white"); buttons.pack(fill="x", padx=30, pady=20)
        def save():
            name = values["name"].get().strip()
            if not name: messagebox.showerror("名称无效", "请输入账号名称。", parent=dialog); return
            if any(i != index and a.get("name") == name for i, a in enumerate(session.accounts)):
                messagebox.showerror("名称重复", "同一平台内账号名称不能重复。", parent=dialog); return
            if account.get("name") in session.account_runtime:
                messagebox.showinfo("请先停止账号", "运行中的账号不能直接修改，请先停止后再编辑。", parent=dialog); return
            account.update(name=name, url=values["url"].get().strip(), video=values["video"].get().strip())
            self.save_config(); dialog.destroy(); self.select_platform(platform_name)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存修改", command=save, style="Primary.TButton").pack(side="right")

    def start_one_account(self, platform_name, account):
        session = self.sessions[platform_name]
        key = account.get("name", "默认账号")
        runtime = session.account_runtime.get(key)
        if runtime and runtime.get("thread") and runtime["thread"].is_alive(): return
        runtime = {"stop": threading.Event(), "process": None, "status": "启动中", "restarts": 0, "error": "", "logs": []}
        session.account_runtime[key] = runtime
        runtime["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 收到启动请求")
        limit = max(0, int(self.max_concurrent_var.get()))
        active = sum(1 for s in self.sessions.values() for r in s.account_runtime.values() if r.get("status") in ("直播中", "启动中", "等待重连"))
        if limit and active >= limit:
            runtime["status"] = "等待启动"
            runtime["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已达到最大并发账号限制（{limit}）")
            self.events.put(("refresh", None, None))
            return
        if not FFMPEG.exists() or not Path(account.get("video", "")).is_file() or not account.get("url", "").startswith(("rtmp://", "rtmps://")):
            runtime["status"] = "配置错误"
            runtime["error"] = "视频文件或 RTMP 地址无效"
            runtime["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动失败：视频文件或 RTMP 地址无效")
            self.events.put(("log", platform_name, f"账号 {key} 配置无效，请检查视频和 RTMP 地址。"))
            self.events.put(("refresh", None, None))
            return
        runtime["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 准备启动 FFmpeg")
        runtime["thread"] = threading.Thread(target=self.account_loop, args=(session, account, runtime), daemon=True)
        runtime["thread"].start()

    def stop_one_account(self, platform_name, account_name):
        runtime = self.sessions[platform_name].account_runtime.get(account_name)
        if runtime:
            runtime["stop"].set()
            if runtime.get("process") and runtime["process"].poll() is None: runtime["process"].terminate()

    def add_account(self, platform_name):
        dialog = tk.Toplevel(self.root)
        body = self.dialog_shell(dialog, "添加推流账号", 560, 330)
        body.pack(fill="both", expand=True)
        values = {}
        for label, key in (("账号名称", "name"), ("推流地址", "url"), ("视频文件", "video")):
            row = tk.Frame(body, bg="white"); row.pack(fill="x", padx=30, pady=7)
            tk.Label(row, text=label, width=10, anchor="w", bg="white", fg="#526174", font=("Microsoft YaHei UI", 10)).pack(side="left")
            values[key] = tk.StringVar()
            entry = ttk.Entry(row, textvariable=values[key], show="*" if key == "url" else "")
            entry.pack(side="left", fill="x", expand=True)
            if key == "video": self.bind_video_drop(entry, values[key])
            if key == "video":
                ttk.Button(row, text="选择", command=lambda: self.pick_account_video(values["video"])).pack(side="left", padx=(8, 0))
        buttons = tk.Frame(body, bg="white"); buttons.pack(fill="x", padx=30, pady=20)
        def create():
            name = values["name"].get().strip()
            if not name:
                messagebox.showerror("名称无效", "请输入账号名称。", parent=dialog); return
            session = self.sessions[platform_name]
            if any(a.get("name") == name for a in session.accounts):
                messagebox.showerror("名称重复", "同一平台内账号名称不能重复。", parent=dialog); return
            session.accounts.append({"name": name, "url": values["url"].get().strip(), "video": values["video"].get().strip(), "enabled": True})
            self.save_config(); dialog.destroy(); self.select_platform(platform_name)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="添加账号", command=create, style="Primary.TButton").pack(side="right")

    def pick_account_video(self, variable):
        path = filedialog.askopenfilename(title="选择账号视频", filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.flv *.ts"), ("所有文件", "*.*")])
        if path: variable.set(path)

    def bind_video_drop(self, widget, variable):
        if DND_FILES is None:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda event: variable.set(event.data.strip().strip("{}")))
        except Exception:
            pass

    def delete_account(self, platform_name):
        selected = self.account_list.curselection()
        if not selected: return
        del self.sessions[platform_name].accounts[selected[0]]
        self.save_config(); self.select_platform(platform_name)

    def save_current(self):
        if not self.current: return
        session = self.sessions[self.current]
        session.url, session.video, session.enabled = self.url_var.get().strip(), self.video_var.get().strip(), self.enabled_var.get()
        self.save_config(); self.refresh_platforms(); self.select_platform()

    def delete_current(self):
        if self.current in BUILTIN_PLATFORMS:
            messagebox.showinfo("预设平台", f"“{self.current}”是预设平台，不能删除。\n可以清空或修改它的推流配置。")
            return
        if self.current and messagebox.askyesno("删除平台", f"确定删除“{self.current}”？"):
            self.stop_session(self.current); del self.sessions[self.current]; self.save_config(); self.show_overview(); self.refresh_platforms()

    def start_session(self, name):
        session = self.sessions[name]
        accounts = session.accounts or [{"name": "默认账号", "url": session.url, "video": session.video, "enabled": True}]
        delay = max(0, int(self.interval_var.get())) * 1000
        selected = [a for a in accounts if a.get("enabled", True)]
        for index, account in enumerate(selected):
            self.root.after(index * delay, lambda a=account: self.start_one_account(name, a))

    def start_all(self):
        for name, session in self.sessions.items():
            if session.enabled: self.start_session(name)

    def stop_session(self, name):
        session = self.sessions.get(name)
        if not session: return
        session.stop_event.set(); session.status = "停止中"
        selected_names = {a.get("name", "") for a in session.accounts if a.get("enabled", True)}
        for account_name, runtime in session.account_runtime.items():
            if account_name in selected_names:
                runtime["stop"].set()
                if runtime.get("process") and runtime["process"].poll() is None: runtime["process"].terminate()

    def stop_all(self):
        for name in list(self.sessions): self.stop_session(name)

    def account_loop(self, session, account, runtime):
        retry = max(3, int(self.retry_var.get()))
        while not runtime["stop"].is_set():
            cmd = [str(FFMPEG), "-hide_banner", "-nostdin", "-loglevel", "warning", "-re", "-stream_loop", "-1", "-i", account.get("video", ""), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "3000k", "-g", "48", "-keyint_min", "48", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-flvflags", "no_duration_filesize", "-f", "flv", account.get("url", "")]
            try:
                runtime["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FFmpeg 进程启动")
                runtime["status"] = "直播中"; session.status = "直播中"
                runtime["process"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in runtime["process"].stdout:
                    if line.strip():
                        runtime.setdefault("logs", []).append(line.strip())
                        runtime["logs"] = runtime["logs"][-500:]
                        self.events.put(("log", session.name + "/" + account.get("name", "账号"), line.strip()))
                code = runtime["process"].wait()
            except OSError as exc:
                code, runtime["error"] = -1, str(exc)
            finally:
                runtime["process"] = None
            if runtime["stop"].is_set() or not self.auto_retry_var.get(): break
            runtime["restarts"] += 1; runtime["status"] = "等待重连"; runtime["error"] = f"错误码 {code}"; runtime["stop"].wait(retry)
        runtime["status"] = "已停止"; session.status = "已停止"
        runtime.setdefault("logs", []).append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 推流任务已停止")
        self.events.put(("refresh", None, None))

    @staticmethod
    def stop_event_wait(session, seconds):
        session.stop_event.wait(seconds)

    def update_overview(self):
        runtimes = [r for s in self.sessions.values() for r in s.account_runtime.values()]
        live = sum(r.get("status") in ("直播中", "启动中", "等待重连") for r in runtimes)
        dropped = sum(r.get("restarts", 0) > 0 for r in runtimes)
        self.live_var.set(str(live)); self.dropped_var.set(str(dropped))
        if hasattr(self, "overview_tree"):
            for item in self.overview_tree.get_children():
                self.overview_tree.delete(item)
            query = self.filter_var.get().strip().lower()
            for s in self.sessions.values():
                if s.account_runtime:
                    for account in s.accounts:
                        key = account.get("name", "默认账号"); runtime = s.account_runtime.get(key, {})
                        status = runtime.get("status", "已停止")
                        values = (s.name, key, status, runtime.get("restarts", 0), Path(account.get("video", "")).name if account.get("video") else "未配置")
                        if query and query not in " ".join(map(str, values)).lower(): continue
                        tag = "live" if status == "直播中" else "error" if status in ("配置错误", "连接失败") else "waiting" if status == "等待重连" else ""
                        self.overview_tree.insert("", "end", values=values, tags=(tag,))
                else:
                    values = (s.name, "-", "已停止", 0, "未配置")
                    if not query or query in s.name.lower(): self.overview_tree.insert("", "end", values=values)

    def poll_events(self):
        if self.closing:
            return
        try:
            while True:
                kind, name, value = self.events.get_nowait()
                if kind == "log" and hasattr(self, "overview_text"):
                    self.overview_text.configure(state=tk.NORMAL)
                    self.overview_text.insert(tk.END, f"[{name}] {value}\n")
                    self.overview_text.see(tk.END)
                    self.overview_text.configure(state=tk.DISABLED)
                elif kind == "refresh":
                    self.update_overview()
                    if self.current in self.sessions: self.select_platform()
                elif kind == "media":
                    self.media_cache[name] = value
                    self.media_pending.discard(name)
                    if self.current in self.sessions:
                        self.select_platform()
                elif kind == "thumb":
                    self.thumb_pending.discard(name)
                    if value:
                        self.thumb_cache[name] = value
                    if self.current in self.sessions:
                        self.select_platform()
        except queue.Empty:
            pass
        if not self.closing and self.root.winfo_exists():
            self.poll_after_id = self.root.after(300, self.poll_events)

    def tray_icon(self):
        try:
            image = Image.open(APP_ICON).convert("RGBA").resize((64, 64)) if APP_ICON.exists() else Image.new("RGB", (64, 64), "#397cf6")
        except (OSError, ValueError):
            image = Image.new("RGB", (64, 64), "#397cf6")
        self.tray = pystray.Icon("挂播工具", image, "挂播工具", pystray.Menu(pystray.MenuItem("显示窗口", lambda *_: self.root.after(0, self.root.deiconify)), pystray.MenuItem("退出", lambda *_: self.root.after(0, self.close))))
        self.tray.run()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.tray: threading.Thread(target=self.tray_icon, daemon=True).start()

    def show_exit_dialog(self):
        dialog = tk.Toplevel(self.root)
        body = self.dialog_shell(dialog, "退出应用", 560, 275)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="退出应用？", bg="white", fg="#172033", font=("Microsoft YaHei UI", 19, "bold"), anchor="w").pack(fill="x", padx=30, pady=(24, 5))
        tk.Label(body, text="请选择退出应用，或让应用继续在系统托盘中运行。", bg="white", fg="#718096", font=("Microsoft YaHei UI", 11), anchor="w").pack(fill="x", padx=30, pady=(0, 20))
        remember = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="记住我的选择", variable=remember, bg="white", fg="#536276", activebackground="white", font=("Microsoft YaHei UI", 10), selectcolor="white").pack(anchor="e", padx=30)
        buttons = tk.Frame(body, bg="white")
        buttons.pack(fill="x", padx=30, pady=25)
        def minimize():
            dialog.destroy(); self.hide_to_tray()
        ttk.Button(buttons, text="最小化到托盘", command=minimize).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="退出应用", command=lambda: (dialog.destroy(), self.close()), style="Primary.TButton").pack(side="right")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def safe_show_exit_dialog(self):
        try:
            self.show_exit_dialog()
        except Exception:
            self.close()

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.poll_after_id:
            try:
                self.root.after_cancel(self.poll_after_id)
            except tk.TclError:
                pass
        self.stop_all()
        if self.tray: self.tray.stop()
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    app = MultiPusherApp(root)
    if not app.already_running:
        try:
            root.mainloop()
        except tk.TclError:
            pass
