"""远程节点（服务端）：Windows 客户端主动用 WebSocket 连上来，插件把每一轮命令派给它执行。

协议要点（全部是 JSON 文本帧）：
- 客户端 → 服务端 hello：node_id / token / machine_id / spool_epoch / 上次离线原因等；
  服务端校验通过回 welcome（带服务端已确认的 last_eid），否则回 reject 并断开。
- 服务端 → 客户端 run / cancel：派活和取消。客户端按 job_id 去重，重复的 run 会被忽略。
- 客户端 → 服务端 事件（带全局递增的 eid）：accepted / out / exit。事件先写进客户端本地 SQLite 队列
  再发送，服务端处理后回 ack(upto)，客户端才删除。断线期间事件攒在本地，重连后按 eid 顺序补发，
  服务端按 eid 去重，所以“不丢、不乱、不重”。
- 心跳：客户端每 N 秒发 hb（附带本地积压条数、正在跑的任务），服务端回 hb_ack。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field

from astrbot.api import logger

from .runner import RunResult
from .utils import human_duration, human_time

PROTO_VERSION = 1


@dataclass
class NodeInfo:
    node_id: str
    online: bool = False
    ws: object = None
    connected_at: float = 0.0
    last_seen: float = 0.0
    offline_since: float = 0.0
    machine_id: str = ""
    hostname: str = ""
    version: str = ""
    peer: str = ""
    spool_pending: int = 0
    running: list = field(default_factory=list)
    reconnects: int = 0
    last_offline_reason: str = ""
    last_boot_unclean: bool = False
    offline_notified: bool = False
    ack_dirty: bool = False
    roots_open: bool = False


class _Job:
    """服务端视角的一条远程执行。"""

    def __init__(self, job_id: str, node_id: str, payload: dict, head_n: int, tail_n: int,
                 on_output=None):
        self.job_id = job_id
        self.node_id = node_id
        self.payload = payload
        self.accepted = False
        self.head: list[str] = []
        self.tail: deque[str] = deque(maxlen=tail_n)
        self.head_n = head_n
        self.lines = 0
        self.on_output = on_output
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()

    def feed(self, lines: list[str]) -> None:
        for line in lines:
            self.lines += 1
            if len(self.head) < self.head_n:
                self.head.append(line)
            else:
                self.tail.append(line)
            if self.on_output:
                try:
                    self.on_output(line, self.lines)
                except Exception:
                    pass

    def to_result(self, ev: dict) -> RunResult:
        total = max(int(ev.get("lines") or 0), self.lines)
        tail = list(self.tail)
        if ev.get("tail") and (not tail or len(ev["tail"]) >= len(tail)):
            tail = [str(x) for x in ev["tail"]][-(self.tail.maxlen or 200):]
            tail = tail[max(0, len(self.head) + len(tail) - total):] if total else tail
        omitted = total - len(self.head) - len(tail)
        parts = list(self.head) + ([f"……（中间省略 {omitted} 行）……"] if omitted > 0 else []) + tail
        code = int(ev.get("exit_code", -1) if ev.get("exit_code") is not None else -1)
        interrupted = bool(ev.get("interrupted"))
        return RunResult(
            ok=code == 0 and not ev.get("timed_out") and not ev.get("cancelled") and not interrupted,
            exit_code=code,
            output="\n".join(parts),
            tail_text="\n".join((self.head + tail)[-(self.tail.maxlen or 200):]),
            duration=float(ev.get("duration") or 0),
            timed_out=bool(ev.get("timed_out")),
            cancelled=bool(ev.get("cancelled")),
            error=str(ev.get("error") or ""),
            lines=total,
            extra={"interrupted": interrupted, "node": self.node_id},
        )


class NodeHub:
    """管理所有远程节点：监听、鉴权、派活、收事件、去重、离线通知。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.nodes: dict[str, NodeInfo] = {}
        self.jobs: dict[str, _Job] = {}
        self._state = {"last_eid": {}, "epoch": {}, "job_task": {}, "pending_cancel": {}}
        self._runner = None
        self._site = None
        self._save_at = 0.0
        self._load_state()
        self._saved_eid = dict(self._state["last_eid"])  # 已经落盘的 last_eid，ack 只能以它为准

    # ---------------- 配置与持久化 ----------------
    def node_cfg(self) -> dict:
        raw = self.plugin.cfg("nodes", {}) or {}
        out = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, str):
                    v = {"token": v}
                if isinstance(v, dict) and str(v.get("token") or "").strip():
                    out[str(k).strip().lower()] = v
        return out

    @property
    def state_path(self) -> str:
        return os.path.join(self.plugin.data_dir, "nodes_state.json")

    def _load_state(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            for k in self._state:
                if isinstance(data.get(k), dict):
                    self._state[k] = data[k]
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[pc_agent] 读取节点状态失败：{exc}")

    def save_state(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._save_at < 1.0:
            return False
        self._save_at = now
        snapshot = dict(self._state["last_eid"])
        jt = self._state["job_task"]
        if len(jt) > 500:  # 只留最近的任务映射
            for k in sorted(jt, key=lambda k: jt[k].get("at", 0))[:-300]:
                jt.pop(k, None)
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._state, fp, ensure_ascii=False)
            with open(tmp, "rb+") as fp:
                os.fsync(fp.fileno())
            os.replace(tmp, self.state_path)
            self._saved_eid = snapshot
            return True
        except Exception as exc:
            logger.warning(f"[pc_agent] 保存节点状态失败：{exc}")
            return False

    def info(self, node_id: str) -> NodeInfo:
        node_id = node_id.lower()
        if node_id not in self.nodes:
            self.nodes[node_id] = NodeInfo(node_id=node_id)
        return self.nodes[node_id]

    def is_online(self, node_id: str) -> bool:
        n = self.nodes.get((node_id or "").lower())
        return bool(n and n.online)

    # ---------------- 监听 ----------------
    async def start(self) -> str:
        if not bool(self.plugin.cfg("node_listen_enable", False)):
            return ""
        try:
            from aiohttp import web
        except Exception as exc:
            return f"没装 aiohttp，远程节点不可用：{exc}"
        host = str(self.plugin.cfg("node_listen_host", "127.0.0.1") or "127.0.0.1")
        port = int(self.plugin.cfg("node_listen_port", 8765) or 8765)
        path = str(self.plugin.cfg("node_ws_path", "/pc-agent/ws") or "/pc-agent/ws")
        ssl_ctx = None
        cert = str(self.plugin.cfg("node_ssl_cert", "") or "")
        key = str(self.plugin.cfg("node_ssl_key", "") or "")
        if cert and key:
            import ssl
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(cert, key)
        app = web.Application()
        app.router.add_get(path, self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port, ssl_context=ssl_ctx, reuse_address=True)
        await self._site.start()
        scheme = "wss" if ssl_ctx else "ws"
        logger.info(f"[pc_agent] 远程节点监听 {scheme}://{host}:{port}{path}")
        return ""

    async def stop(self) -> None:
        for n in self.nodes.values():
            if n.ws is not None:
                try:
                    await n.ws.close()
                except Exception:
                    pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self.save_state(force=True)

    async def _send(self, node: NodeInfo, msg: dict) -> bool:
        if not node.online or node.ws is None:
            return False
        try:
            await node.ws.send_str(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception as exc:
            logger.info(f"[pc_agent] 发给节点 {node.node_id} 失败：{exc}")
            return False

    async def _handle(self, request):
        from aiohttp import WSMsgType, web
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        peer = request.headers.get("X-Forwarded-For", "") or (request.remote or "")
        node: NodeInfo | None = None
        flusher = None
        try:
            first = await ws.receive(timeout=20)
            if first.type != WSMsgType.TEXT:
                return ws
            hello = json.loads(first.data)
            node_id, err = self._auth(hello)
            if err:
                logger.warning(f"[pc_agent] 拒绝节点注册（{peer}）：{err}")
                await ws.send_str(json.dumps({"type": "reject", "reason": err}, ensure_ascii=False))
                await ws.close()
                return ws
            node = self.info(node_id)
            old = node.ws
            await self._on_connect(node, ws, hello, peer)
            if old is not None and old is not ws:
                try:
                    await old.close()  # 断线后服务端还没察觉的旧连接，直接替换
                except Exception:
                    pass
            flusher = asyncio.create_task(self._ack_loop(node, ws))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                        break
                    continue
                node.last_seen = time.time()
                try:
                    await self._on_message(node, ws, json.loads(msg.data))
                except Exception:
                    logger.exception(f"[pc_agent] 处理节点 {node.node_id} 消息出错")
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.info(f"[pc_agent] 节点连接异常：{exc}")
        finally:
            if flusher is not None:
                flusher.cancel()
            if node is not None and node.ws is ws:
                node.online = False
                node.ws = None
                node.offline_since = time.time()
                node.offline_notified = False
                logger.info(f"[pc_agent] 节点 {node.node_id} 断开")
                self.save_state(force=True)
        return ws

    async def _ack_loop(self, node: NodeInfo, ws) -> None:
        """攒一小批事件 → 先把 last_eid 落盘（fsync）→ 再回 ack。保证不会“ack 了却没落盘”。"""
        while True:
            await asyncio.sleep(0.3)
            if not node.ack_dirty:
                continue
            node.ack_dirty = False
            if not self.save_state(force=True):
                node.ack_dirty = True
                continue
            try:
                await ws.send_str(json.dumps(
                    {"type": "ack", "upto": int(self._saved_eid.get(node.node_id, 0))}))
            except Exception:
                return

    def _auth(self, hello: dict) -> tuple[str, str]:
        if hello.get("type") != "hello":
            return "", "第一条消息必须是 hello"
        node_id = str(hello.get("node_id") or "").strip().lower()
        conf = self.node_cfg().get(node_id)
        if conf is None:
            return "", f"未登记的节点「{node_id}」"
        if not hmac.compare_digest(str(conf.get("token")), str(hello.get("token") or "")):
            return "", f"节点「{node_id}」token 不对"
        pinned = str(conf.get("machine_id") or "").strip().lower()
        if pinned and pinned != str(hello.get("machine_id") or "").strip().lower():
            return "", f"节点「{node_id}」机器码不匹配（配置要求 {pinned}）"
        return node_id, ""

    async def _on_connect(self, node: NodeInfo, ws, hello: dict, peer: str) -> None:
        epoch = str(hello.get("spool_epoch") or "")
        if self._state["epoch"].get(node.node_id) != epoch:
            # 客户端的本地队列是新建的（重装/删库），eid 从头算
            self._state["epoch"][node.node_id] = epoch
            self._state["last_eid"][node.node_id] = 0
        was_offline_for = time.time() - node.offline_since if node.offline_since else 0
        node.ws, node.online, node.peer = ws, True, peer
        node.connected_at = node.last_seen = time.time()
        node.machine_id = str(hello.get("machine_id") or "")
        node.hostname = str(hello.get("hostname") or "")
        node.version = str(hello.get("version") or "")
        node.spool_pending = int(hello.get("spool_pending") or 0)
        node.last_offline_reason = str(hello.get("last_offline_reason") or "")
        node.last_boot_unclean = bool(hello.get("last_boot_unclean"))
        node.roots_open = bool(hello.get("roots_unrestricted"))
        if node.roots_open:
            logger.warning(f"[pc_agent] 节点 {node.node_id} 没有配置 allowed_roots：服务端派来的任何目录都会执行")
        node.reconnects += 1
        self.save_state(force=True)
        last = int(self._saved_eid.get(node.node_id, 0))
        await ws.send_str(json.dumps({"type": "welcome", "proto": PROTO_VERSION,
                                      "last_eid": last}, ensure_ascii=False))
        logger.info(f"[pc_agent] 节点 {node.node_id} 上线（{peer}，积压 {node.spool_pending} 条，"
                    f"离线 {human_duration(was_offline_for)}，原因：{node.last_offline_reason or '-'}）")
        # 断线期间没送达的派活 / 取消，补发一次
        for job in list(self.jobs.values()):
            if job.node_id == node.node_id and not job.accepted and not job.future.done():
                await self._send(node, job.payload)
        for jid in list(self._state["pending_cancel"].get(node.node_id, [])):
            await self._send(node, {"type": "cancel", "job_id": jid})
        self._state["pending_cancel"][node.node_id] = []
        self.save_state(force=True)
        if node.offline_notified:
            node.offline_notified = False
            await self.plugin.push(
                f"🔌 节点 {node.node_id} 重新连上了（离线 {human_duration(was_offline_for)}），"
                f"正在补发 {node.spool_pending} 条积压输出。", self.plugin.tasks.last_umo)

    async def _on_message(self, node: NodeInfo, ws, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "hb":
            node.spool_pending = int(msg.get("spool_pending") or 0)
            node.running = list(msg.get("running") or [])
            await ws.send_str(json.dumps({"type": "hb_ack", "t": msg.get("t")}))
            return
        if mtype != "event":
            return
        eid = int(msg.get("eid") or 0)
        last = int(self._state["last_eid"].get(node.node_id, 0))
        if eid and eid <= last:
            node.ack_dirty = True  # 重复补发的，丢掉；由 _ack_loop 按已落盘的位置回 ack
            return
        ev = msg.get("data") or {}
        await self._apply_event(node, ev)
        if eid:
            self._state["last_eid"][node.node_id] = eid
        node.ack_dirty = True

    async def _apply_event(self, node: NodeInfo, ev: dict) -> None:
        kind, jid = ev.get("kind"), str(ev.get("job_id") or "")
        job = self.jobs.get(jid)
        if kind == "accepted":
            if job:
                job.accepted = True
            return
        if kind == "out":
            if job:
                job.feed([str(x) for x in ev.get("lines") or []])
            return
        if kind != "exit":
            return
        if job and not job.future.done():
            job.future.set_result(job.to_result(ev))
            return
        # 没有人在等这条结果：插件重启过，或者早就被 /pc stop 放弃了
        meta = self._state["job_task"].get(jid)
        if meta and not ev.get("cancelled"):
            tail_n = getattr(getattr(self.plugin, "runner", None), "tail_lines", 200)
            result = _Job(jid, node.node_id, {}, 0, tail_n).to_result(ev)
            await self.plugin.on_orphan_result(meta, result, jid)

    # ---------------- 派活 ----------------
    async def run_job(self, node_id: str, job_id: str, task_id: str, kind: str, argv, cwd, env,
                      timeout, stdin_text=None, cancel: asyncio.Event | None = None,
                      on_output=None, shell_command=None, head_n=20, tail_n=200) -> RunResult:
        node_id = node_id.lower()
        if node_id not in self.node_cfg():
            return RunResult(ok=False, exit_code=-1, error=f"节点「{node_id}」没有在 nodes 里登记")
        payload = {"type": "run", "job_id": job_id, "argv": list(argv or []), "cwd": cwd,
                   "env": dict(env or {}), "timeout": int(timeout or 1800), "stdin": stdin_text,
                   "shell_command": shell_command or None, "kind": kind}
        job = _Job(job_id, node_id, payload, head_n, tail_n, on_output)
        self.jobs[job_id] = job
        self._state["job_task"][job_id] = {"task_id": task_id, "kind": kind, "node": node_id,
                                           "at": time.time()}
        self.save_state(force=True)
        node = self.info(node_id)
        if not await self._send(node, payload):
            logger.info(f"[pc_agent] 节点 {node_id} 不在线，{job_id} 等它连上再派")
        waiters = {job.future}
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
        if cancel_task:
            waiters.add(cancel_task)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if job.future.done():
                return job.future.result()
            if self.plugin.tasks.shutting_down:
                # 插件重载 / 关闭：不取消远端，让 Windows 那边跑完，结果之后按“孤儿结果”补回任务
                return RunResult(ok=False, exit_code=-1, cancelled=True, lines=job.lines)
            # 主人叫停：能发就发取消，发不出去就记着，等节点上线再发
            if not await self._send(node, {"type": "cancel", "job_id": job_id}):
                self._state["pending_cancel"].setdefault(node_id, []).append(job_id)
                self.save_state(force=True)
            return RunResult(ok=False, exit_code=-1, cancelled=True, lines=job.lines,
                             output="\n".join(job.head + list(job.tail)))
        except asyncio.CancelledError:
            # 插件关闭时不取消远端：让 Windows 那边跑完，结果重启后按“孤儿结果”处理
            if not self.plugin.tasks.shutting_down and \
                    not await self._send(node, {"type": "cancel", "job_id": job_id}):
                self._state["pending_cancel"].setdefault(node_id, []).append(job_id)
            self.save_state(force=True)
            raise
        finally:
            if cancel_task and not cancel_task.done():
                cancel_task.cancel()
            self.jobs.pop(job_id, None)

    # ---------------- 巡检与展示 ----------------
    async def watch(self) -> None:
        """长时间离线、且有任务在等它时，提醒一次（校园网频繁闪断不刷屏）。"""
        threshold = int(self.plugin.cfg("node_offline_notify_minutes", 10) or 0) * 60
        if threshold <= 0:
            return
        now = time.time()
        for node_id in self.node_cfg():
            n = self.info(node_id)
            if n.online or n.offline_notified:
                continue
            since = n.offline_since or n.connected_at
            waiting = [j for j in self.jobs.values() if j.node_id == node_id]
            if since and waiting and now - since > threshold:
                n.offline_notified = True
                await self.plugin.push(
                    f"📴 节点 {node_id} 已经离线 {human_duration(now - since)}，有 {len(waiting)} 条执行在等它。\n"
                    f"断网期间那边的任务会照常跑完，输出存在本地，连上后自动补发；不用操作。",
                    self.plugin.tasks.last_umo)

    def describe(self) -> str:
        conf = self.node_cfg()
        if not conf:
            return "没有登记任何远程节点（插件配置 nodes）。"
        lines = [f"远程节点（监听：{'开' if self.plugin.cfg('node_listen_enable', False) else '关'}）："]
        now = time.time()
        for node_id in conf:
            n = self.info(node_id)
            waiting = [j for j in self.jobs.values() if j.node_id == node_id]
            unsent = sum(1 for j in waiting if not j.accepted)
            if n.online:
                state = f"🟢 在线 {human_duration(now - n.connected_at)}，上次心跳 {int(now - n.last_seen)} 秒前"
            elif n.offline_since:
                state = f"🔴 离线 {human_duration(now - n.offline_since)}（{human_time(n.offline_since)} 起）"
            else:
                state = "⚪ 本次启动后还没连上来过"
            lines.append(f"· {node_id} {state}")
            if n.hostname or n.version:
                lines.append(f"  机器：{n.hostname or '-'} · 客户端 v{n.version or '?'} · 来自 {n.peer or '-'}")
            lines.append(f"  节点本地待补发：{n.spool_pending} 条 · 节点上在跑：{len(n.running)} 个"
                         f" · 服务端在等结果：{len(waiting)} 个（其中未送达 {unsent} 个）")
            pc = self._state["pending_cancel"].get(node_id) or []
            if pc:
                lines.append(f"  待送达的取消：{len(pc)} 个")
            if n.last_offline_reason:
                lines.append(f"  上次断线原因（客户端判断）：{n.last_offline_reason}")
            if n.roots_open:
                lines.append("  ⚠️ 节点没有配置 allowed_roots（未限制目录）：服务端派来的任何目录都会执行")
            if n.last_boot_unclean:
                lines.append("  ⚠️ 客户端上次是非正常退出（断电/崩溃）")
            if n.machine_id:
                lines.append(f"  机器码：{n.machine_id}"
                             + ("" if conf[node_id].get("machine_id") else "（可填进 machine_id 锁定这台机器）"))
            lines.append(f"  累计连接 {n.reconnects} 次")
        return "\n".join(lines)
