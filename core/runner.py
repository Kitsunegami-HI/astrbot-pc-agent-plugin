"""子进程执行器：跑命令、收集输出、超时/取消时杀干净整个进程树。"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field

from astrbot.api import logger

from .utils import decode_line, strip_ansi

# cmd.exe 会解释的字符：参数里带这些又只能走 shell 时，宁可拒绝也不冒注入的险
_CMD_META = re.compile(r'[&|<>^%!"\r\n]')
# npm 在 Windows 上生成的 xxx.cmd 垫片里指向真正脚本的那一段
_NPM_SHIM = re.compile(r'"%(?:~)?dp0%?\\?([^"%]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)
_READ_CHUNK = 65536
_MAX_LINE = 2000


@dataclass
class RunResult:
    """一次执行的结果。output 是“开头 + 省略提示 + 结尾”，tail_text 只有结尾。"""

    ok: bool
    exit_code: int
    output: str = ""
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    lines: int = 0
    tail_text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def launched(self) -> bool:
        """进程是不是真的起来了（找不到命令之类的情况算没起来）。"""
        return not (self.exit_code == -1 and self.error and not self.lines)


def _unwrap_npm_shim(path: str) -> list[str] | None:
    """把 npm 的 .cmd 垫片还原成 `node 脚本.js`，这样就不用经过 cmd.exe。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fp:
            content = fp.read(8192)
    except OSError:
        return None
    m = _NPM_SHIM.search(content)
    if not m:
        return None
    base = os.path.dirname(path)
    script = os.path.normpath(os.path.join(base, m.group(1)))
    if not os.path.isfile(script):
        return None
    local_node = os.path.join(base, "node.exe")
    node = local_node if os.path.isfile(local_node) else (shutil.which("node") or "")
    return [node, script] if node else None


def resolve_executable(argv: list[str]) -> tuple[list[str], bool, str]:
    """找到真正的可执行文件。返回 (argv, 是否必须经过 shell, 错误信息)。"""
    if not argv:
        return argv, False, "命令是空的"
    path = shutil.which(argv[0]) or argv[0]
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        direct = _unwrap_npm_shim(path)
        if direct:
            return [*direct, *argv[1:]], False, ""
        bad = [a for a in argv[1:] if _CMD_META.search(a)]
        if bad:
            return argv, True, (
                f"{os.path.basename(path)} 只能经过 cmd.exe 启动，而指令里含有引号、&、%、换行等"
                f"会被 cmd 当成命令解释的字符。请把这个软件改成 \"prompt_via\": \"stdin\"，"
                f"或在 cmd 里直接写真正的可执行文件路径（node xxx.js / xxx.exe）。")
        return [path, *argv[1:]], True, ""
    return [path, *argv[1:]], False, ""


class ProcessRunner:
    """负责真正把命令跑起来。"""

    def __init__(self, tail_lines: int = 200, head_lines: int = 40):
        self.tail_lines = max(50, int(tail_lines or 200))
        self.head_lines = max(0, int(head_lines or 0))

    async def run(self, argv, cwd=None, env=None, timeout=1800, stdin_text=None,
                  cancel: asyncio.Event | None = None, on_output=None,
                  shell_command: str | None = None) -> RunResult:
        """执行一条命令。

        on_output(line, line_count) 用于实时回调；shell_command 不为空时忽略 argv，
        直接交给系统 shell（只给用户自己配置的验收命令用，不会拼入任何 AI 指令）。
        """
        start = time.time()
        if shell_command:
            via_shell, cmdline = True, shell_command
        else:
            argv, via_shell, err = resolve_executable(list(argv))
            if err:
                return RunResult(ok=False, exit_code=-1, error=err)
            cmdline = subprocess.list2cmdline(argv) if via_shell else ""

        child_env = os.environ.copy()
        child_env.update({
            "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb", "CI": "1",
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        })
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})

        head: list[str] = []
        tail: deque[str] = deque(maxlen=self.tail_lines)
        state = {"lines": 0}

        kwargs = dict(
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd or None,
            env=child_env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True  # 自成进程组，停的时候整组带走
        try:
            if via_shell:
                proc = await asyncio.create_subprocess_shell(cmdline, **kwargs)
            else:
                proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except FileNotFoundError:
            name = argv[0] if not shell_command else shell_command
            return RunResult(ok=False, exit_code=-1, error=f"找不到可执行文件：{name}",
                             duration=time.time() - start)
        except Exception as exc:
            return RunResult(ok=False, exit_code=-1, error=f"启动失败：{exc}",
                             duration=time.time() - start)

        def emit(raw: bytes):
            line = strip_ansi(decode_line(raw)).rstrip("\n")
            for piece in line.split("\n"):
                if len(piece) > _MAX_LINE:
                    piece = piece[:_MAX_LINE] + f"…（本行太长，截掉 {len(piece) - _MAX_LINE} 字）"
                state["lines"] += 1
                if len(head) < self.head_lines:
                    head.append(piece)
                else:
                    tail.append(piece)
                if on_output:
                    try:
                        on_output(piece, state["lines"])
                    except Exception:
                        pass

        async def reader():
            # 按块读、自己切行：一行超长（比如压缩过的 JSON）也不会让 readline 抛异常
            stream = proc.stdout
            buf = b""
            while stream is not None:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                *done, buf = buf.split(b"\n")
                for raw in done:
                    emit(raw)
                if len(buf) > 1_000_000:  # 没有换行的超长输出，硬切
                    emit(buf)
                    buf = b""
            if buf:
                emit(buf)

        async def feeder():
            if stdin_text is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as exc:
                logger.debug(f"[pc_agent] 写入 stdin 失败：{exc}")
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        read_task = asyncio.create_task(reader())
        feed_task = asyncio.create_task(feeder())
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None

        timed_out = cancelled = False
        try:
            waiters = {wait_task} | ({cancel_task} if cancel_task else set())
            done, _ = await asyncio.wait(waiters, timeout=max(30, int(timeout or 0)),
                                         return_when=asyncio.FIRST_COMPLETED)
            if wait_task not in done:
                if cancel_task is not None and cancel_task in done:
                    cancelled = True
                else:
                    timed_out = True
                await self.kill(proc)
            # 主进程退了，但孙进程可能还占着输出管道：最多再等 5 秒
            try:
                await asyncio.wait_for(asyncio.shield(read_task), timeout=5)
            except asyncio.TimeoutError:
                await self.kill(proc)
                read_task.cancel()
        except asyncio.CancelledError:
            await self.kill(proc)
            raise
        finally:
            for t in (read_task, feed_task, wait_task, cancel_task):
                if t is not None and not t.done():
                    t.cancel()

        total = state["lines"]
        tail_list = list(tail)
        omitted = total - len(head) - len(tail_list)
        parts = list(head)
        if omitted > 0:
            parts.append(f"……（中间省略 {omitted} 行）……")
        parts.extend(tail_list)
        code = proc.returncode if proc.returncode is not None else -1
        return RunResult(
            ok=(not timed_out) and (not cancelled) and code == 0,
            exit_code=code,
            output="\n".join(parts),
            tail_text="\n".join((head + tail_list)[-self.tail_lines:]),
            duration=time.time() - start,
            timed_out=timed_out,
            cancelled=cancelled,
            lines=total,
        )

    @staticmethod
    async def kill(proc) -> None:
        """连子进程一起杀掉，避免留下的孙进程继续占额度。"""
        if os.name == "nt":
            if proc.returncode is None:
                try:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await asyncio.wait_for(killer.wait(), timeout=15)
                except Exception as exc:
                    logger.debug(f"[pc_agent] taskkill 失败：{exc}")
                    try:
                        proc.kill()
                    except Exception:
                        pass
        else:
            # 即使主进程已经退出，进程组里也可能还有孙进程
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:
                logger.debug(f"[pc_agent] 结束进程组失败：{exc}")
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass
