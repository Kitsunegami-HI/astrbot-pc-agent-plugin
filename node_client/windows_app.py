"""Windows 节点的图形入口，使用与命令行客户端相同的 WebSocket 协议。"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from urllib.parse import urlsplit

import pc_agent_node as node


TITLE = "AstrBot 远程节点"
STATUS_TEXT = {
    "idle": "未连接", "waiting": "等待网络就绪", "connecting": "正在连接",
    "online": "已注册并在线", "retrying": "连接中断，正在重试",
    "stopping": "正在断开", "error": "连接已停止",
}


def app_config(settings: dict, app_dir: Path) -> dict:
    cfg = {**node.DEFAULTS, **settings}
    url = urlsplit(cfg["server_url"])
    if (url.scheme != "wss" or not url.hostname or url.username is not None
            or url.fragment or url.query or url.port == 0
            or any(char.isspace() for char in cfg["server_url"])):
        raise ValueError("请填写完整的 wss:// 地址，例如 wss://bot.example.com/pc-agent/ws")
    path = url.path if url.path and url.path != "/" else "/pc-agent/ws"
    cfg["server_url"] = url._replace(path=path).geturl()
    if not cfg["node_id"] or not cfg["token"]:
        raise ValueError("请填写与 AstrBot 插件配置一致的节点名和 Token。")
    if not cfg["token"].isascii():
        raise ValueError("请使用由英文字母、数字或 ASCII 符号组成的 Token。")
    if not cfg["allowed_roots"]:
        raise ValueError("请添加至少一个节点允许访问的项目目录。")
    for folder in cfg["allowed_roots"]:
        if not os.path.isabs(folder) or not os.path.isdir(folder):
            raise ValueError(f"请选择本机存在的绝对路径：{folder}")
    identity = json.dumps([url.hostname, url.port or 443, path, cfg["node_id"].lower()])
    profile = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    cfg["data_dir"] = str(app_dir / "node_data" / profile)
    cfg["insecure_skip_verify"] = False
    return cfg


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as fp:
            temporary = Path(fp.name)
            json.dump(settings, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class NodeWorker:
    def __init__(self, cfg: dict, updates: queue.SimpleQueue):
        self.cfg = cfg
        self.updates = updates
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    async def run_until_stopped(self) -> None:
        def report(state, detail):
            self.updates.put((state, detail))

        task = asyncio.create_task(node.amain(self.cfg, report))
        try:
            while not task.done() and not self.stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def run(self) -> None:
        error = ""
        try:
            data_dir = self.cfg["data_dir"]
            os.makedirs(data_dir, exist_ok=True)
            lock = node.single_instance(data_dir)
            if lock is None:
                raise RuntimeError("这个节点已在另一个窗口运行。")
            with lock:
                handlers = node.setup_logging(data_dir)
                try:
                    asyncio.run(self.run_until_stopped())
                finally:
                    for handler in handlers:
                        node.log.removeHandler(handler)
                        handler.close()
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            self.updates.put(("finished", error))


class App:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.settings = {}
        self.worker = None
        self.data_dir = None
        self.closing = False
        self.updates = queue.SimpleQueue()
        load_error = ""
        try:
            self.settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(self.settings, dict):
                raise ValueError("配置文件必须是 JSON 对象")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.settings = {}
            load_error = f"配置读取失败，请检查后重试：{exc}"
        self.url = tk.StringVar(value=self.settings.get("server_url", ""))
        self.node_id = tk.StringVar(value=self.settings.get("node_id", ""))
        self.token = tk.StringVar(value=self.settings.get("token", ""))
        self.status = tk.StringVar(value=STATUS_TEXT["idle"])
        self.detail = tk.StringVar(value=load_error)

        root.title(TITLE)
        root.minsize(580, 415)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=(24, 20))
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        family = font.nametofont("TkDefaultFont").actual("family")
        ttk.Label(frame, text=TITLE, font=(family, 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        fields = [("服务器地址", self.url, ""), ("节点名", self.node_id, ""), ("Token", self.token, "*")]
        self.entries = []
        for row, (label, value, mask) in zip((1, 3, 4), fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16))
            entry = ttk.Entry(frame, textvariable=value, show=mask, width=44)
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5, ipady=3)
            self.entries.append(entry)
        self.entries[0].focus_set()
        ttk.Label(frame, text="例如 wss://bot.example.com/pc-agent/ws").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Label(frame, text="允许的目录").grid(row=5, column=0, sticky="nw", pady=5)
        self.roots = tk.Text(frame, height=3, width=30, wrap="none", font="TkDefaultFont",
                             padx=5, pady=4, undo=True)
        self.roots.grid(row=5, column=1, sticky="ew", pady=(5, 0))
        folders = self.settings.get("allowed_roots")
        if isinstance(folders, list) and all(isinstance(folder, str) for folder in folders):
            self.roots.insert("1.0", "\n".join(folders))
        self.choose = ttk.Button(frame, text="添加…", command=self.add_folder)
        self.choose.grid(row=5, column=2, sticky="ne", padx=(10, 0), pady=5)
        ttk.Label(frame, text="每行一个项目目录，例如 D:\\code").grid(
            row=6, column=1, columnspan=2, sticky="w", pady=(3, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.connect = ttk.Button(buttons, text="保存并连接", command=self.start)
        self.connect.pack(side="left")
        self.disconnect = ttk.Button(buttons, text="断开", command=self.stop, state="disabled")
        self.disconnect.pack(side="left", padx=8)
        self.show_log = ttk.Button(buttons, text="打开日志目录", command=self.open_logs, state="disabled")
        self.show_log.pack(side="right")

        ttk.Separator(frame).grid(row=8, column=0, columnspan=3, sticky="ew", pady=(16, 9))
        ttk.Label(frame, textvariable=self.status, font=(family, 11, "bold")).grid(
            row=9, column=0, columnspan=3, sticky="w")
        detail = ttk.Label(frame, textvariable=self.detail, wraplength=520, justify="left")
        detail.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        frame.bind("<Configure>", lambda event: detail.configure(wraplength=max(1, event.width - 48)))
        root.after(150, self.refresh)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            folder = os.path.normpath(folder)
            existing = self.roots.get("1.0", "end-1c").strip()
            if folder not in existing.splitlines():
                self.roots.insert("end-1c", f"\n{folder}" if existing else folder)

    def start(self) -> None:
        settings = {**self.settings, "server_url": self.url.get().strip(),
                    "node_id": self.node_id.get().strip(), "token": self.token.get().strip(),
                    "allowed_roots": [line.strip() for line in self.roots.get("1.0", "end").splitlines()
                                      if line.strip()]}
        try:
            cfg = app_config(settings, self.config_path.parent)
            settings["server_url"] = cfg["server_url"]
            save_settings(self.config_path, settings)
        except (ValueError, OSError) as exc:
            self.detail.set(str(exc))
            return
        self.settings = settings
        self.url.set(cfg["server_url"])
        self.data_dir = cfg["data_dir"]
        self.worker = NodeWorker(cfg, self.updates)
        self.worker.thread.start()
        self.set_editable(False)
        self.disconnect.config(state="normal")
        self.set_status("connecting")

    def set_editable(self, editable: bool) -> None:
        state = "normal" if editable else "disabled"
        for widget in (*self.entries, self.roots, self.choose, self.connect):
            widget.config(state=state)

    def set_status(self, state: str, detail: str = "") -> None:
        self.status.set(STATUS_TEXT[state])
        self.detail.set(detail)

    def stop(self) -> None:
        if self.worker:
            self.disconnect.config(state="disabled")
            self.set_status("stopping", "正在结束本轮任务并保存待补发的输出。")
            self.worker.stop.set()

    def close(self) -> None:
        if self.worker:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def refresh(self) -> None:
        while self.worker:
            try:
                state, detail = self.updates.get_nowait()
            except queue.Empty:
                break
            if state == "finished":
                if self.closing:
                    self.root.destroy()
                    return
                self.worker = None
                self.disconnect.config(state="disabled")
                self.set_editable(True)
                self.set_status("error" if detail else "idle", detail)
            elif not self.worker.stop.is_set():
                self.set_status(state, detail)
        self.show_log.config(state="normal" if self.data_dir and Path(self.data_dir).is_dir() else "disabled")
        self.root.after(150, self.refresh)

    def open_logs(self) -> None:
        try:
            os.startfile(self.data_dir)
        except OSError as exc:
            messagebox.showerror(TITLE, str(exc), parent=self.root)


def main() -> None:
    if getattr(sys, "frozen", False):
        # 让外部 CLI 按自己的 DLL 搜索路径启动。
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    app_dir = Path(os.environ["LOCALAPPDATA"]) / "AstrBotPCAgent"
    root = tk.Tk()
    root.withdraw()
    try:
        app_dir.mkdir(parents=True, exist_ok=True)
        lock = node.single_instance(str(app_dir))
        if lock is None:
            messagebox.showinfo(TITLE, "节点窗口已经打开。", parent=root)
            return
        with lock:
            App(root, app_dir / "client_config.json")
            root.deiconify()
            root.mainloop()
    except OSError as exc:
        messagebox.showerror(TITLE, str(exc), parent=root)


if __name__ == "__main__":
    main()
