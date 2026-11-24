"""pc_agent_node：远程电脑代理的 Windows 节点客户端。

在你自己的电脑上常驻运行，主动用 wss 连到 AstrBot 服务器，接收指令、在本机执行，
把输出流式回传。专门为“校园网经常断 + 宿舍半夜拉闸断电”设计：

- 断网时任务照常在本机跑完，输出先写进本地 SQLite 队列（每批都 fsync 落盘），
  重连后按顺序补发，服务端确认（ack）后才删除——不丢、不乱、不重。
- 断电（直接拉闸）后再开机：上次没跑完的任务会被识别出来，明确上报“被中断”，
  绝不当成正常结束；服务端按策略决定续跑还是等你回话。
- 断线后指数退避 + 随机抖动无限重试，永不退出；开机冷启动时先等网络（路由器、校园网认证）就绪，
  只记一行“等待网络就绪”日志，不刷屏。
- 用心跳和网络探测区分“本机没网 / 校园网要重新认证 / 服务器挂了 / 服务端握手失败”，写进日志。

依赖：Python 3.9+，pip install websockets
用法：python pc_agent_node.py --config client_config.json
      python pc_agent_node.py --check   （只读检查：不建目录、不写日志，结果打到屏幕）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid
from collections import deque
from urllib.parse import urlparse

VERSION = "0.3.5"
IS_WIN = os.name == "nt"
log = logging.getLogger("pc_node")

DEFAULTS = {
    "server_url": "wss://example.com/pc-agent/ws",
    "node_id": "mypc",
    "token": "",
    "data_dir": "",                 # 默认：脚本旁边的 node_data
    "allowed_commands": ["codex", "claude", "aider", "npm", "npx", "node", "python", "py",
                         "pytest", "cargo", "go", "git", "tsc", "pnpm", "yarn"],
    "allowed_roots": [],            # 允许的工作目录根。空 = 不限制目录（启动日志和 /pc node 会明确警告）
    "allow_shell": False,           # 是否允许服务端下发 shell 形式的验收命令
    "ca_file": "",                  # 自签证书时填 CA/证书路径
    "insecure_skip_verify": False,  # 不校验证书（不推荐）
    "allow_plain_ws": False,        # 允许 ws://（只建议在内网测试时用）
    "heartbeat_seconds": 15,
    "heartbeat_timeout_seconds": 60,
    "cold_start_delay_seconds": 75,  # 开机后多久内算“冷启动”，先等这么久再连
    "cold_start_uptime_seconds": 600,
    "backoff_min_seconds": 5,
    "backoff_max_seconds": 300,
    "keep_awake": True,
    "tail_lines": 200,
    "max_lines_per_job": 20000,     # 单个任务最多往队列里写多少行（超出只保留结尾，防止撑爆磁盘）
    "probe_hosts": ["223.5.5.5:53", "119.29.29.29:53", "114.114.114.114:53"],
}


# ====================================================================== 工具
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CMD_META = re.compile(r'[&|<>^%!"\r\n]')
_NPM_SHIM = re.compile(r'"%(?:~)?dp0%?\\?([^"%]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)


def clean_line(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    text = _CTRL_RE.sub("", _ANSI_RE.sub("", text))
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def uptime_seconds() -> float:
    try:
        if IS_WIN:
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        with open("/proc/uptime") as fp:
            return float(fp.read().split()[0])
    except Exception:
        return 1e9


def machine_id() -> str:
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0]).lower()
        with open("/etc/machine-id") as fp:
            return fp.read().strip().lower()
    except Exception:
        return ""


def proc_token(pid) -> str:
    """进程还活着就返回它的“启动时间指纹”（防 pid 复用），否则返回空串。"""
    if not pid:
        return ""
    try:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes as wt
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wt.HANDLE
            k.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                code = wt.DWORD()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                    return ""
                ft = [wt.FILETIME() for _ in range(4)]
                if not k.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                    return "alive"
                return f"{ft[0].dwHighDateTime}:{ft[0].dwLowDateTime}"
            finally:
                k.CloseHandle(h)
        with open(f"/proc/{int(pid)}/stat") as fp:
            fields = fp.read().rsplit(")", 1)[1].split()
        return "" if fields[0] in ("Z", "X") else fields[19]  # 第 22 个字段：启动时刻
    except Exception:
        return ""


def kill_pid_tree(pid: int) -> None:
    """同步地结束一个进程及其子进程（恢复时清理上一轮的残留进程用）。"""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except Exception:
                os.kill(int(pid), signal.SIGKILL)
    except Exception as exc:
        log.warning(f"结束残留进程 {pid} 失败：{exc}")


def keep_awake(on: bool) -> None:
    """常驻期间阻止系统自动睡眠（不影响关屏）。"""
    if not IS_WIN:
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception as exc:
        log.warning(f"设置防睡眠失败：{exc}")


def resolve_argv(argv: list[str]) -> tuple[list[str], bool, str]:
    """返回 (argv, 是否要走 shell, 错误)。npm 的 .cmd 垫片还原成 node xxx.js，避免经过 cmd.exe。"""
    if not argv:
        return argv, False, "命令是空的"
    path = shutil.which(argv[0]) or argv[0]
    if IS_WIN and path.lower().endswith((".cmd", ".bat")):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                m = _NPM_SHIM.search(fp.read(8192))
        except OSError:
            m = None
        if m:
            base = os.path.dirname(path)
            script = os.path.normpath(os.path.join(base, m.group(1)))
            node = os.path.join(base, "node.exe")
            node = node if os.path.isfile(node) else (shutil.which("node") or "")
            if node and os.path.isfile(script):
                return [node, script, *argv[1:]], False, ""
        if any(_CMD_META.search(a) for a in argv[1:]):
            return argv, True, (f"{os.path.basename(path)} 只能经过 cmd.exe 启动，而参数里有会被 cmd 解释的字符；"
                                f"请在服务端把这个软件改成 prompt_via=stdin")
        return [path, *argv[1:]], True, ""
    return [path, *argv[1:]], False, ""


# ====================================================================== 本地队列
class Spool:
    """SQLite 持久化：发件箱（事件）+ 任务表。每次写入都提交并 fsync（synchronous=FULL），扛得住拉闸。"""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS outbox(eid INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, payload TEXT, status TEXT,
                                            started REAL, finished REAL);
        """)
        for col in ("pid INTEGER", "pstart TEXT"):  # 0.3.0 的旧库补列
            try:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.new_event = asyncio.Event()
        if not self.get("epoch"):
            self.set("epoch", uuid.uuid4().hex)

    def get(self, k, default=""):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def add_event(self, data: dict) -> int:
        cur = self.db.execute("INSERT INTO outbox(data) VALUES(?)", (json.dumps(data, ensure_ascii=False),))
        self.new_event.set()
        return cur.lastrowid

    def events_after(self, eid: int, limit: int = 200):
        return self.db.execute("SELECT eid, data FROM outbox WHERE eid>? ORDER BY eid LIMIT ?",
                               (eid, limit)).fetchall()

    def ack(self, upto: int) -> None:
        self.db.execute("DELETE FROM outbox WHERE eid<=?", (int(upto),))

    def pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def job(self, job_id):
        return self.db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def job_start(self, job_id, payload):
        self.db.execute("INSERT OR REPLACE INTO jobs(job_id,payload,status,started,finished,pid,pstart) "
                        "VALUES(?,?,?,?,NULL,NULL,NULL)",
                        (job_id, json.dumps(payload, ensure_ascii=False), "running", time.time()))

    def job_set_pid(self, job_id, pid, token):
        self.db.execute("UPDATE jobs SET pid=?, pstart=? WHERE job_id=?", (pid, token, job_id))

    def job_finish(self, job_id, status):
        self.db.execute("UPDATE jobs SET status=?, finished=? WHERE job_id=?", (status, time.time(), job_id))
        # 只留最近 500 条任务记录用来去重
        self.db.execute("DELETE FROM jobs WHERE job_id NOT IN "
                        "(SELECT job_id FROM jobs ORDER BY started DESC LIMIT 500)")

    def unfinished_jobs(self):
        return self.db.execute(
            "SELECT job_id, started, pid, pstart FROM jobs WHERE status='running'").fetchall()


# ====================================================================== 执行
class Executor:
    def __init__(self, cfg: dict, spool: Spool):
        self.cfg = cfg
        self.spool = spool
        self.cancels: dict[str, asyncio.Event] = {}
        self.pre_cancel: set[str] = set()  # 取消比派活先到（或派活还没起进程）时，先记下取消意图
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping = False

    def recover_after_crash(self) -> list[str]:
        """上次没跑完的任务（断电/崩溃/被杀）：明确上报“被中断”，不当成正常结束。"""
        lost = []
        for job_id, started, pid, token in self.spool.unfinished_jobs():
            begin = time.strftime('%m-%d %H:%M', time.localtime(started or 0))
            alive = bool(pid) and bool(token) and proc_token(pid) == token
            if alive:
                # 客户端自己重启了，但上一轮的命令还活着：输出管道已经断了，没法接管，
                # 先结束它，避免和续跑的新一轮在同一个项目里撞车
                kill_pid_tree(int(pid))
                error = (f"节点客户端重启时发现这一轮的进程（pid {pid}，开始于 {begin}）还在跑，"
                         f"输出已经接不上了；为避免同一项目跑出两个进程，已先结束它")
                log.warning(f"[{job_id}] {error}")
            else:
                error = (f"节点客户端在这一轮运行中被中断（断电 / 关机 / 崩溃），开始于 {begin}，"
                         f"本机在 {time.strftime('%m-%d %H:%M')} 重新启动后发现")
            self.spool.add_event({
                "kind": "exit", "job_id": job_id, "exit_code": -1, "interrupted": True,
                "error": error,
                "lines": 0, "tail": [], "duration": 0})
            self.spool.job_finish(job_id, "interrupted")
            lost.append(job_id)
        return lost

    def running(self) -> list[str]:
        return [j for j, t in self.tasks.items() if not t.done()]

    def submit(self, payload: dict) -> None:
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return
        if self.spool.job(job_id):
            log.info(f"[{job_id}] 重复的派活，忽略（结果会通过队列补发）")
            return
        self.spool.job_start(job_id, payload)
        self.spool.add_event({"kind": "accepted", "job_id": job_id})
        ev = asyncio.Event()
        if job_id in self.pre_cancel:
            self.pre_cancel.discard(job_id)
            ev.set()
        self.cancels[job_id] = ev  # 提交即注册，进程还没起来时的取消也不会丢
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, payload))

    def cancel(self, job_id: str) -> None:
        ev = self.cancels.get(job_id)
        if ev:
            ev.set()
        else:
            if len(self.pre_cancel) > 1000:
                self.pre_cancel.clear()
            self.pre_cancel.add(job_id)

    async def shutdown(self) -> None:
        self.stopping = True
        for ev in self.cancels.values():
            ev.set()
        await asyncio.gather(*(task for task in self.tasks.values() if not task.done()),
                             return_exceptions=True)

    def _check(self, payload: dict) -> str:
        cwd = str(payload.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            return f"工作目录不存在：{cwd or '（空）'}"
        # realpath 会解开符号链接和 junction，root 里放一个指向别处的 junction 也出不去
        roots = [os.path.normcase(os.path.realpath(r)) for r in self.cfg.get("allowed_roots") or []]
        if roots:
            real = os.path.normcase(os.path.realpath(cwd))
            def inside(r):
                try:
                    return os.path.commonpath([real, r]) == r
                except ValueError:  # 不同盘符
                    return False
            if not any(inside(r) for r in roots):
                return f"工作目录 {cwd}（实际指向 {real}）不在客户端允许的 allowed_roots 里"
        if payload.get("shell_command"):
            if not self.cfg.get("allow_shell"):
                return "客户端没有开启 allow_shell，拒绝执行 shell 形式的命令"
            return ""
        argv = payload.get("argv") or []
        if not argv:
            return "命令是空的"
        allowed = {a.lower() for a in self.cfg.get("allowed_commands") or []}
        exe = os.path.splitext(os.path.basename(str(argv[0])))[0].lower()
        if allowed and exe not in allowed:
            return f"命令「{exe}」不在客户端的 allowed_commands 里"
        return ""

    async def _run(self, job_id: str, p: dict) -> None:
        start = time.time()
        tail = deque(maxlen=int(self.cfg.get("tail_lines", 200)))
        state = {"lines": 0, "buf": [], "flushed": time.time(), "capped": False}
        max_lines = int(self.cfg.get("max_lines_per_job", 20000))

        def flush():
            if state["buf"]:
                self.spool.add_event({"kind": "out", "job_id": job_id, "lines": state["buf"]})
                state["buf"] = []
            state["flushed"] = time.time()

        cancel = self.cancels.setdefault(job_id, asyncio.Event())

        def finish(**kw):
            flush()
            self.cancels.pop(job_id, None)
            data = {"kind": "exit", "job_id": job_id, "lines": state["lines"], "tail": list(tail),
                    "duration": round(time.time() - start, 1)}
            data.update(kw)
            self.spool.add_event(data)
            self.spool.job_finish(job_id, "done")
            log.info(f"[{job_id}] 结束：退出码 {data.get('exit_code')}，{state['lines']} 行，"
                     f"用时 {int(data['duration'])} 秒")

        err = self._check(p)
        if err:
            log.warning(f"[{job_id}] 拒绝执行：{err}")
            finish(exit_code=-1, error=err)
            return
        shell_cmd = p.get("shell_command")
        if shell_cmd:
            argv, via_shell = [], True
        else:
            argv, via_shell, err = resolve_argv([str(x) for x in p.get("argv") or []])
            if err:
                finish(exit_code=-1, error=err)
                return
        env = os.environ.copy()
        env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb", "CI": "1",
                    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        env.update({str(k): str(v) for k, v in (p.get("env") or {}).items()})
        stdin_text = p.get("stdin")
        kw = dict(stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                  cwd=p.get("cwd"), env=env)
        if IS_WIN:
            kw["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kw["start_new_session"] = True
        if cancel.is_set():
            log.info(f"[{job_id}] 还没启动就被取消了")
            if self.stopping:
                finish(exit_code=-1, interrupted=True, error="节点断开时这一轮尚未启动")
            else:
                finish(exit_code=-1, cancelled=True, error="还没启动就被取消了")
            return
        log.info(f"[{job_id}] 开始：{shell_cmd or ' '.join(argv)[:200]}  @ {p.get('cwd')}")
        try:
            if via_shell:
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd or subprocess.list2cmdline(argv), **kw)
            else:
                proc = await asyncio.create_subprocess_exec(*argv, **kw)
        except FileNotFoundError:
            finish(exit_code=-1, error=f"找不到可执行文件：{argv[0] if argv else shell_cmd}")
            return
        except Exception as exc:
            finish(exit_code=-1, error=f"启动失败：{exc}")
            return

        self.spool.job_set_pid(job_id, proc.pid, proc_token(proc.pid))

        def emit(raw: bytes):
            for line in clean_line(raw).split("\n"):
                if len(line) > 2000:
                    line = line[:2000] + "…（本行太长已截断）"
                state["lines"] += 1
                tail.append(line)
                if state["lines"] <= max_lines:
                    state["buf"].append(line)
                elif not state["capped"]:
                    state["capped"] = True
                    state["buf"].append(f"……（输出超过 {max_lines} 行，后面只保留结尾）……")
            if len(state["buf"]) >= 200 or time.time() - state["flushed"] > 0.5:
                flush()

        async def reader():
            buf = b""
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                *done, buf = buf.split(b"\n")
                for raw in done:
                    emit(raw)
                if len(buf) > 1_000_000:
                    emit(buf)
                    buf = b""
            if buf:
                emit(buf)

        async def feeder():
            if stdin_text is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(str(stdin_text).encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def ticker():  # 输出很慢时也按时落盘
            while True:
                await asyncio.sleep(1)
                if state["buf"]:
                    flush()

        helpers = [asyncio.create_task(x) for x in (reader(), feeder(), ticker())]
        wait_t = asyncio.create_task(proc.wait())
        cancel_t = asyncio.create_task(cancel.wait())
        timed_out = cancelled = False
        try:
            done, _ = await asyncio.wait({wait_t, cancel_t}, timeout=max(30, int(p.get("timeout") or 1800)),
                                         return_when=asyncio.FIRST_COMPLETED)
            if wait_t not in done:
                cancelled = cancel_t in done
                timed_out = not cancelled
                await kill_tree(proc)
            try:
                await asyncio.wait_for(asyncio.shield(helpers[0]), timeout=5)
            except asyncio.TimeoutError:
                await kill_tree(proc)
        finally:
            for t in helpers + [wait_t, cancel_t]:
                if not t.done():
                    t.cancel()
            self.cancels.pop(job_id, None)
        code = proc.returncode if proc.returncode is not None else -1
        if cancelled and self.stopping:
            finish(exit_code=-1, interrupted=True, error="Windows 节点主动断开，此轮任务已中断")
        else:
            finish(exit_code=code, timed_out=timed_out, cancelled=cancelled)


async def kill_tree(proc) -> None:
    try:
        if IS_WIN:
            k = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            await asyncio.wait_for(k.wait(), timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception as exc:
        log.debug(f"结束进程树失败：{exc}")
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        pass


# ====================================================================== 网络诊断
async def _tcp_ok(host: str, port: int, timeout: float = 4) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def _captive_portal() -> bool | None:
    """访问微软的联网检测地址：内容不对 = 被校园网登录页劫持了。None = 根本连不上。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("www.msftconnecttest.com", 80), 5)
        w.write(b"GET /connecttest.txt HTTP/1.1\r\nHost: www.msftconnecttest.com\r\nConnection: close\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(4096), 5)
        w.close()
        return b"Microsoft Connect Test" not in data
    except Exception:
        return None


async def diagnose(cfg: dict, host: str, port: int) -> str:
    """断线 / 连不上时判断原因：本机没网、校园网要认证、服务器挂了、还是服务端握手失败。"""
    probes = []
    for hp in cfg.get("probe_hosts") or []:
        h, _, p = str(hp).rpartition(":")
        probes.append(_tcp_ok(h, int(p or 53)))
    results = await asyncio.gather(*probes) if probes else []
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port), 5)
        dns_ok = True
    except Exception:
        dns_ok = False
    if not any(results) and not dns_ok:
        portal = await _captive_portal()
        if portal:
            return "NET_PORTAL: 校园网需要重新认证（联网检测被重定向到登录页）"
        return "NET_DOWN: 本机没网（网线/Wi-Fi 未连接、路由器还没启动好、或校园网整体断了）"
    if await _captive_portal():
        return "NET_PORTAL: 校园网需要重新认证（联网检测被重定向到登录页）"
    if not dns_ok:
        return f"DNS_FAIL: 外网通，但解析不了服务器域名 {host}"
    if not await _tcp_ok(host, port, 6):
        return f"SERVER_DOWN: 本机外网正常，但服务器 {host}:{port} 连不上（服务器挂了 / 端口被封 / 反代没起）"
    return "SERVER_REJECT: 服务器端口能连上，但 WebSocket 握手或鉴权失败（插件没开监听、路径/证书/token 不对）"


# ====================================================================== 连接
class Node:
    def __init__(self, cfg: dict, spool: Spool, executor: Executor, on_status=None):
        self.cfg = cfg
        self.spool = spool
        self.ex = executor
        self.on_status = on_status
        u = urlparse(cfg["server_url"])
        self.host = u.hostname or ""
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.last_reason = spool.get("last_offline_reason", "")
        self.unclean = False

    def status(self, state: str, detail: str = "") -> None:
        if self.on_status is not None:
            self.on_status(state, detail)

    def ssl_ctx(self):
        if not self.cfg["server_url"].startswith("wss://"):
            return None
        ctx = ssl.create_default_context(cafile=self.cfg.get("ca_file") or None)
        if self.cfg.get("insecure_skip_verify"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def forever(self) -> None:
        import websockets
        backoff_min = float(self.cfg["backoff_min_seconds"])
        backoff_max = float(self.cfg["backoff_max_seconds"])
        delay = backoff_min
        # —— 冷启动：断电后来电，电脑比路由器 / 校园网认证先就绪，先安静地等一会儿 ——
        up = uptime_seconds()
        if up < float(self.cfg["cold_start_uptime_seconds"]):
            wait = max(0.0, float(self.cfg["cold_start_delay_seconds"]) - up) + random.uniform(0, 15)
            if wait > 0:
                log.info(f"开机不久（{int(up)} 秒），等待网络就绪：{int(wait)} 秒后开始连接，期间不再刷日志")
                self.status("waiting", f"约 {int(wait)} 秒后开始连接")
                await asyncio.sleep(wait)
            delay = max(delay, 15.0)  # 冷启动从较低频率开始退避
        fails, first_fail_at, logged_reason = 0, 0.0, ""
        while True:
            started = time.time()
            reason = ""
            connected = False
            self.status("connecting")
            try:
                async with websockets.connect(
                        self.cfg["server_url"], ssl=self.ssl_ctx(), open_timeout=20,
                        ping_interval=20, ping_timeout=20, max_size=16 * 1024 * 1024,
                        close_timeout=5) as ws:
                    if fails:
                        log.info(f"已连上服务器（之前连续失败 {fails} 次，断了 "
                                 f"{int(time.time() - first_fail_at)} 秒）")
                    fails, logged_reason, connected = 0, "", True
                    reason = await self.session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            self.status("retrying", reason[:200])
            # —— 断了：诊断原因，只在原因变化时写日志 ——
            lived = time.time() - started
            if lived > 60:
                delay = backoff_min  # 稳定连过一阵，退避从头算
            diag = await diagnose(self.cfg, self.host, self.port)
            if reason.startswith("REJECT"):
                diag = reason
            elif connected and diag.startswith("SERVER_REJECT"):
                diag = f"FLAP: 连接中途断开（{reason[:120]}），现在网络和服务器都可达，马上重连"
                delay = backoff_min
            self.last_reason = f"{diag}（{time.strftime('%m-%d %H:%M')}）"
            self.spool.set("last_offline_reason", self.last_reason)
            if fails == 0:
                first_fail_at = time.time()
            fails += 1
            code = diag.split(":")[0]
            if code != logged_reason or fails % 50 == 0:
                log.warning(f"与服务器断开 / 连不上：{diag}；细节：{reason[:200]}；"
                            f"本地待补发 {self.spool.pending()} 条，正在跑 {len(self.ex.running())} 个任务，"
                            f"将无限重试（第 {fails} 次）")
                logged_reason = code
            if code == "REJECT":
                delay = backoff_max  # 被拒是配置问题，别狂刷
            sleep = random.uniform(delay * 0.5, delay)  # 抖动，避免和别的程序扎堆重试
            self.status("retrying", f"{diag[:200]}，约 {max(1, round(sleep))} 秒后重试")
            await asyncio.sleep(sleep)
            delay = min(backoff_max, delay * 2)

    async def session(self, ws) -> str:
        hello = {"type": "hello", "node_id": self.cfg["node_id"], "token": self.cfg["token"],
                 "version": VERSION, "machine_id": machine_id(), "hostname": socket.gethostname(),
                 "spool_epoch": self.spool.get("epoch"), "spool_pending": self.spool.pending(),
                 "running": self.ex.running(), "last_offline_reason": self.last_reason,
                 "last_boot_unclean": self.unclean,
                 "roots_unrestricted": not (self.cfg.get("allowed_roots") or [])}
        await ws.send(json.dumps(hello, ensure_ascii=False))
        first = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if first.get("type") == "reject":
            log.error(f"服务器拒绝注册：{first.get('reason')}（检查 node_id / token / machine_id）")
            return f"REJECT: 服务器拒绝注册：{first.get('reason')}"
        if first.get("type") != "welcome":
            return "SERVER_REJECT: 服务器回了意料之外的消息"
        self.spool.ack(int(first.get("last_eid") or 0))
        log.info(f"注册成功，开始补发本地积压 {self.spool.pending()} 条")
        self.status("online")
        self.unclean = False
        state = {"last_rx": time.time()}

        async def sender():
            sent = int(first.get("last_eid") or 0)
            while True:
                self.spool.new_event.clear()
                rows = self.spool.events_after(sent)
                for eid, data in rows:
                    await ws.send(json.dumps({"type": "event", "eid": eid, "data": json.loads(data)},
                                             ensure_ascii=False))
                    sent = eid
                if not rows:
                    await self.spool.new_event.wait()

        async def heartbeat():
            hb = float(self.cfg["heartbeat_seconds"])
            timeout = float(self.cfg["heartbeat_timeout_seconds"])
            while True:
                await ws.send(json.dumps({"type": "hb", "t": time.time(),
                                          "spool_pending": self.spool.pending(),
                                          "running": self.ex.running()}))
                await asyncio.sleep(hb)
                if time.time() - state["last_rx"] > timeout:
                    raise ConnectionError(f"HEARTBEAT: {int(timeout)} 秒没收到服务器任何回应")

        async def receiver():
            async for raw in ws:
                state["last_rx"] = time.time()
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "ack":
                    self.spool.ack(int(msg.get("upto") or 0))
                elif t == "run":
                    self.ex.submit(msg)
                elif t == "cancel":
                    log.info(f"[{msg.get('job_id')}] 服务端要求取消")
                    self.ex.cancel(str(msg.get("job_id")))
            raise ConnectionError("服务器关闭了连接")

        tasks = [asyncio.create_task(x) for x in (sender(), heartbeat(), receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    return f"{type(t.exception()).__name__}: {t.exception()}"
            return "连接结束"
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ====================================================================== 入口
def setup_logging(data_dir: str) -> list[logging.Handler]:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(os.path.join(data_dir, "node.log"), maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    handlers = [fh]
    if sys.stderr is not None:  # pythonw.exe 下没有控制台
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
        handlers.append(sh)
    return handlers


def single_instance(data_dir: str):
    fp = open(os.path.join(data_dir, "node.lock"), "a+")
    try:
        if IS_WIN:
            import msvcrt
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return None
    return fp


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as fp:
        cfg = {**DEFAULTS, **json.load(fp)}
    if not cfg.get("data_dir"):
        cfg["data_dir"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_data")
    return cfg


async def amain(cfg: dict, on_status=None) -> None:
    spool = Spool(os.path.join(cfg["data_dir"], "spool.db"))
    unclean = spool.get("clean_exit", "1") != "1"
    spool.set("clean_exit", "0")
    ex = Executor(cfg, spool)
    lost = ex.recover_after_crash()
    if unclean:
        log.warning("上次是非正常退出（断电 / 强制关机 / 崩溃）")
    if not cfg.get("allowed_roots"):
        log.warning("⚠️ 没有配置 allowed_roots：服务端派来的任何工作目录都会执行（未限制目录）。"
                    "强烈建议在 client_config.json 里填上项目所在的目录")
    if lost:
        log.warning(f"上次有 {len(lost)} 个任务没跑完，已记为“被中断”并排队上报：{', '.join(lost)}")
    node = Node(cfg, spool, ex, on_status)
    node.unclean = unclean
    if cfg.get("keep_awake"):
        keep_awake(True)
    try:
        await node.forever()
    finally:
        try:
            await ex.shutdown()
            if not spool.unfinished_jobs():
                spool.set("clean_exit", "1")
        finally:
            if cfg.get("keep_awake"):
                keep_awake(False)
            spool.db.close()


def config_problems(cfg: dict) -> list[str]:
    """会导致客户端无法正常工作的配置问题（启动和 --check 共用同一套判断）。"""
    out = []
    if not cfg.get("token") or not cfg.get("node_id") or "example.com" in str(cfg.get("server_url")):
        out.append("请先在 client_config.json 里填好 server_url / node_id / token")
    url = str(cfg.get("server_url") or "")
    if url.startswith("ws://") and not cfg.get("allow_plain_ws"):
        out.append("server_url 是明文 ws://，token 会被看见。请用 wss://（或在测试时设 allow_plain_ws=true）")
    elif not url.startswith(("ws://", "wss://")):
        out.append(f"server_url 必须以 wss:// 开头：{url}")
    return out


def run_check(config_path: str) -> int:
    """--check：纯只读检查。不建 data_dir、不写日志、不改本地队列，结果只打到 stdout。"""
    print(f"pc_agent_node v{VERSION} 配置检查（只读：不建目录、不写日志）")
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ 找不到配置文件：{config_path}")
        return 2
    except Exception as exc:
        print(f"✗ 配置文件读不了：{exc}")
        return 2
    problems = config_problems(cfg)
    for msg in problems:
        print(f"✗ {msg}")
    roots = cfg.get("allowed_roots") or []
    if not roots:
        print("⚠ 没有配置 allowed_roots：服务端派来的任何工作目录都会执行（未限制目录）")
    for r in roots:
        if not os.path.isdir(r):
            print(f"⚠ allowed_roots 里的目录不存在：{r}")
    if cfg.get("allow_shell"):
        print("⚠ 已开启 allow_shell：服务端可以下发整串 shell 命令")
    data_dir = cfg["data_dir"]
    db = os.path.join(data_dir, "spool.db")
    if not os.path.isdir(data_dir):
        print(f"· 数据目录还不存在（首次正式运行时才会创建）：{data_dir}")
    elif not os.path.isfile(db):
        print(f"· 数据目录存在，但还没有本地队列：{data_dir}")
    else:
        try:
            from urllib.request import pathname2url
            con = sqlite3.connect(f"file:{pathname2url(os.path.abspath(db))}?mode=ro", uri=True)
            try:
                pending = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                row = con.execute("SELECT v FROM meta WHERE k='clean_exit'").fetchone()
                running = con.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            finally:
                con.close()
            print(f"· 本地待补发：{pending} 条；记录为“运行中”的任务：{running} 个")
            if row and row[0] != "1":
                print("· 客户端正在运行，或上次是非正常退出（断电 / 崩溃）")
        except Exception as exc:
            print(f"· 本地队列读不了（不影响检查）：{exc}")
    if problems:
        return 2
    u = urlparse(cfg["server_url"])
    print("网络：" + asyncio.run(diagnose(cfg, u.hostname or "", u.port or (443 if u.scheme == "wss" else 80))))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="远程电脑代理 · Windows 节点客户端")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "client_config.json"))
    ap.add_argument("--check", action="store_true",
                    help="只读检查配置、本地队列和网络后退出：不建目录、不写日志、不常驻")
    args = ap.parse_args()
    if args.check:
        sys.exit(run_check(args.config))
    cfg = load_config(args.config)
    os.makedirs(cfg["data_dir"], exist_ok=True)
    setup_logging(cfg["data_dir"])
    problems = config_problems(cfg)
    if problems:
        for msg in problems:
            log.error(msg)
        sys.exit(2)
    lock = single_instance(cfg["data_dir"])
    if lock is None:
        log.info("已经有一个客户端在运行，本次退出")
        return
    log.info(f"pc_agent_node v{VERSION} 启动，节点 {cfg['node_id']} → {cfg['server_url']}")
    while True:  # 最外层兜底：任何意外都不退出
        try:
            asyncio.run(amain(cfg))
        except KeyboardInterrupt:
            log.info("收到退出信号，正常退出")
            return
        except Exception:
            log.exception("客户端主循环异常，10 秒后重启")
            time.sleep(10)


if __name__ == "__main__":
    main()
