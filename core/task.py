"""任务状态机：跑一轮 → 识别额度 → 验收 → 自主续轮 / 等主人回话；以及排队和额度恢复后的续跑。"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

from astrbot.api import logger

from . import limits
from .software import SoftwareSpec
from .utils import human_duration, human_time, preview, safe_int


class TaskStatus:
    """任务状态常量。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting_input"
    QUOTA = "quota_wait"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.QUOTA)
STATUS_TEXT = {
    TaskStatus.PENDING: "排队中",
    TaskStatus.RUNNING: "跑着",
    TaskStatus.WAITING: "等你回话",
    TaskStatus.QUOTA: "等额度",
    TaskStatus.DONE: "完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.STOPPED: "已停",
}
QUOTA_PREFIX = "（上一次执行因为额度用完被中断了）请继续完成这条指令："
MAX_RECORDS = 50


@dataclass
class Task:
    """一个远程任务。"""

    id: str = ""
    software: str = ""
    instruction: str = ""          # 下一轮要交给它的指令
    goal: str = ""                 # 整个任务的目标（自主模式和验收修复都靠它）
    autopilot: bool = False
    max_rounds: int = 6
    status: str = TaskStatus.PENDING
    rounds: int = 0                # 实际跑完的轮数（撞额度的那轮不算）
    round_base: int = 0            # 本次自主预算从第几轮开始算（主人每接一次话就重置）
    umo: str = ""
    workdir: str = ""
    project: str = ""
    node: str = ""                 # 远程节点名，空 = 本机
    created_at: float = 0.0
    updated_at: float = 0.0
    resume_at: float = 0.0
    session_started: bool = False  # 是否已经有可以接续的会话
    session_id: str = ""           # 从输出里抓到的会话 id（配置了 session_pattern 时）
    last_output: str = ""
    last_error: str = ""
    last_verify: str = ""          # 最近一次验收命令的输出
    verify_ok: bool = False        # 最近一次验收是否通过
    verify_attempts: int = 0       # 连续验收失败次数
    notified_restart: bool = True  # 重启打断后是否已经通知过主人
    history: list = field(default_factory=list)

    @property
    def short(self) -> str:
        """给主人看的短编号。"""
        return (self.id or "????")[:6]

    def is_active(self) -> bool:
        return self.status in ACTIVE


class TaskManager:
    """管理所有任务的创建、执行、暂停、排队与续跑。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.tasks: dict[str, Task] = {}
        self.last_umo = ""
        self.shutting_down = False
        self._cancel: dict[str, asyncio.Event] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()
        self._injected: dict[str, tuple] = {}  # 插件重启期间节点跑完的结果，接回流程用

    # ---------------- 持久化 ----------------
    @property
    def store_path(self) -> str:
        return os.path.join(self.plugin.data_dir, "tasks.json")

    def _prune(self) -> None:
        """进行中的全留，结束的只留最近 MAX_RECORDS 条。"""
        finished = [t for t in self.tasks.values() if not t.is_active()]
        finished.sort(key=lambda t: t.updated_at or t.created_at)
        for t in finished[:-MAX_RECORDS] if len(finished) > MAX_RECORDS else []:
            self.tasks.pop(t.id, None)

    def save(self) -> None:
        try:
            self._prune()
            data = [asdict(t) for t in self.tasks.values()]
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=1)
            os.replace(tmp, self.store_path)
        except Exception as exc:
            logger.warning(f"[pc_agent] 保存任务失败：{exc}")

    def load(self) -> None:
        """启动时把上次的任务读回来；被打断的转成“等你回话”，并记下要通知主人。"""
        if not os.path.isfile(self.store_path):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as exc:
            logger.warning(f"[pc_agent] 读取任务失败：{exc}")
            return
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                task = Task(**{k: v for k, v in item.items() if k in Task.__dataclass_fields__})
            except Exception:
                continue
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.WAITING
                task.last_error = "AstrBot 重启 / 插件重载，这一轮被打断"
                task.notified_restart = False
            self.tasks[task.id] = task
            if task.umo:
                self.last_umo = task.umo
        logger.info(f"[pc_agent] 恢复 {len(self.tasks)} 条任务记录")

    # ---------------- 创建与调度 ----------------
    def new(self, software: str, instruction: str, umo: str = "", autopilot: bool = False,
            workdir: str = "", project: str = "", node: str = "") -> Task:
        now = time.time()
        task = Task(
            id=uuid.uuid4().hex[:8],
            software=str(software).lower(),
            instruction=(instruction or "").strip(),
            goal=(instruction or "").strip(),
            autopilot=bool(autopilot),
            max_rounds=safe_int(self.plugin.cfg("autopilot_max_rounds", 6), 6, 1),
            umo=umo or self.last_umo,
            workdir=workdir,
            project=project,
            node=node,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task.id] = task
        if umo:
            self.last_umo = umo
        self.save()
        return task

    def busy_with(self, task: Task) -> Task | None:
        """同一个目录里是不是已经有别的任务在跑（同目录并发会互相改乱文件）。"""
        if not bool(self.plugin.cfg("queue_same_dir", True)):
            return None
        for other in self.tasks.values():
            if other.id != task.id and other.status == TaskStatus.RUNNING \
                    and other.node == task.node \
                    and os.path.normcase(other.workdir) == os.path.normcase(task.workdir):
                return other
        return None

    def submit(self, task: Task) -> Task | None:
        """开跑；同目录有别的任务在跑就先排队。返回挡路的那个任务（没排队则 None）。"""
        blocker = self.busy_with(task)
        if blocker is not None:
            task.status = TaskStatus.PENDING
            task.updated_at = time.time()
            self.save()
            return blocker
        self.launch(task)
        return None

    def launch(self, task: Task) -> None:
        """丢到后台跑，不阻塞消息处理。"""
        old = self._runners.get(task.id)
        if old and not old.done():
            old.cancel()
        task.status = TaskStatus.RUNNING
        task.updated_at = time.time()
        rt = asyncio.create_task(self._drive(task))
        self._runners[task.id] = rt

        def _cleanup(t, tid=task.id):
            if self._runners.get(tid) is t:
                self._runners.pop(tid, None)
        rt.add_done_callback(_cleanup)

    def kick_queue(self) -> None:
        """有目录空出来了，就把排队的任务按先来后到放出去。"""
        if self.shutting_down:
            return
        pending = sorted((t for t in self.tasks.values() if t.status == TaskStatus.PENDING),
                         key=lambda t: t.updated_at)
        for task in pending:
            if self.busy_with(task) is None:
                self.launch(task)
                self._spawn(self.plugin.push(f"▶️ {task.short} 排到了，开始跑。", task.umo))

    def _spawn(self, coro) -> None:
        """后台小任务，留个引用防止被回收。"""
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _drive(self, task: Task) -> None:
        try:
            await self._round_loop(task)
        except asyncio.CancelledError:
            if self.shutting_down:
                task.status = TaskStatus.WAITING
                task.last_error = "插件重载 / AstrBot 关闭，这一轮被打断"
                task.notified_restart = False
            elif task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.STOPPED
            self.save()
            raise
        except Exception as exc:
            logger.exception(f"[pc_agent] 任务 {task.short} 异常")
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.save()
            await self.plugin.push(f"❌ 任务 {task.short} 出错了：{exc}", task.umo)
        finally:
            self._cancel.pop(task.id, None)
            if not self.shutting_down:
                asyncio.get_running_loop().call_soon(self.kick_queue)

    # ---------------- 单轮执行 ----------------
    def _autopilot_budget_left(self, task: Task, started: float) -> str:
        """自主模式还能不能再跑一轮；不能就返回原因。"""
        used = task.rounds - task.round_base
        if used >= task.max_rounds:
            return f"本次已经连跑 {used} 轮，到了 autopilot_max_rounds={task.max_rounds} 的上限"
        max_minutes = safe_int(self.plugin.cfg("autopilot_max_minutes", 120), 120, 1)
        if time.time() - started > max_minutes * 60:
            return f"本次已经跑了 {human_duration(time.time() - started)}，到了 {max_minutes} 分钟的时长上限"
        return ""

    def _prompt_for_round(self, task: Task, resumed: bool) -> str:
        """拼出这一轮真正交给软件的文字。接不上会话时，把总目标一起带上。"""
        if task.session_started and not resumed and task.goal and task.instruction != task.goal:
            return (f"这是一个持续进行中的任务（之前的会话接不上了，请先看一下项目当前状态）。\n"
                    f"总目标：{task.goal}\n本轮要求：{task.instruction}")
        return task.instruction

    async def _exec(self, task: Task, spec: SoftwareSpec, kind: str, argv, **kw):
        """执行一条命令：本机直接跑，配了 node 的派给远程节点。"""
        if not spec.node:
            return await self.plugin.runner.run(argv, cwd=task.workdir, env=spec.env, **kw)
        hub = self.plugin.node_hub
        if not hub.is_online(spec.node):
            await self.plugin.push(
                f"📴 {task.short}：节点 {spec.node} 现在不在线，这一步（{'验收' if kind == 'verify' else '执行'}）"
                f"先记着，等它连上自动派过去。/pc node 看状态。", task.umo)
        job_id = f"{task.id}-{task.rounds + 1}-{kind}-{uuid.uuid4().hex[:4]}"
        return await hub.run_job(
            spec.node, job_id, task.id, kind, argv, task.workdir, spec.env,
            kw.get("timeout"), stdin_text=kw.get("stdin_text"), cancel=kw.get("cancel"),
            on_output=kw.get("on_output"), shell_command=kw.get("shell_command"),
            head_n=self.plugin.runner.head_lines, tail_n=self.plugin.runner.tail_lines)

    async def _run_verify(self, task: Task, spec: SoftwareSpec, cancel: asyncio.Event):
        cfg_timeout = spec.verify_timeout or 600
        if spec.verify_shell:
            return await self._exec(task, spec, "verify", [], timeout=cfg_timeout, cancel=cancel,
                                    shell_command=" ".join(spec.verify))
        return await self._exec(task, spec, "verify", spec.build_verify(), timeout=cfg_timeout,
                                cancel=cancel)

    async def _round_loop(self, task: Task) -> None:
        """一轮一轮地跑，直到需要主人回话 / 额度耗尽 / 目标结束。"""
        plugin = self.plugin
        spec: SoftwareSpec | None = plugin.specs.get(task.software)
        if spec is None:
            task.status = TaskStatus.FAILED
            task.last_error = f"软件 {task.software} 不在白名单"
            self.save()
            await plugin.push(f"❌ 白名单里没有「{task.software}」，先在插件配置的 software 里加上它。",
                              task.umo)
            return
        if not task.workdir or (not spec.node and not os.path.isdir(task.workdir)):
            task.status = TaskStatus.FAILED
            task.last_error = f"工作目录不存在：{task.workdir or '（空）'}"
            self.save()
            await plugin.push(f"❌ {task.short} 的{task.last_error}", task.umo)
            return

        started = time.time()
        first = True
        interrupts = 0
        sentinel = str(plugin.cfg("autopilot_sentinel", "__DONE__") or "__DONE__")
        live = safe_int(plugin.cfg("live_push_seconds", 0), 0, 0)
        default_timeout = safe_int(plugin.cfg("default_timeout", 1800), 1800, 30)

        while True:
            # 自主模式：每一轮开跑前都检查预算（验收重试的轮次也算）
            if task.autopilot and not first:
                why = self._autopilot_budget_left(task, started)
                if why:
                    task.status = TaskStatus.WAITING
                    self.save()
                    await plugin.push(
                        f"🏁 {task.short} 自主模式先停：{why}。\n"
                        f"要接着干：/pc go {task.short} [补充指令]；不管了：/pc done {task.short}",
                        task.umo)
                    return
            first = False

            # 插件重启期间节点跑完的那一轮：结果接回正常流程（额度识别、验收照常）
            inj = self._injected.pop(task.id, None)
            if inj and inj[0] == "verify":
                cancel = asyncio.Event()
                self._cancel[task.id] = cancel
                step = await self._verify_gate(task, spec, cancel, v_res=inj[1])
                if step == "retry":
                    continue
                if step == "stop":
                    return
                if await self._after_round(task, spec, started, sentinel) == "continue":
                    continue
                return

            _, _, resumed = spec.build("", resume=task.session_started, session_id=task.session_id)
            prompt = self._prompt_for_round(task, resumed)
            argv, stdin_text, resumed = spec.build(
                prompt, resume=task.session_started, session_id=task.session_id)
            round_no = task.rounds + 1
            cancel = asyncio.Event()
            self._cancel[task.id] = cancel
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self.save()

            live_state = {"last": time.time(), "buf": []}

            def on_output(line, _count, _s=live_state):
                """运行中按间隔把最近几行推过去（默认关）。"""
                if live <= 0:
                    return
                _s["buf"].append(line)
                del _s["buf"][:-15]
                if time.time() - _s["last"] >= live and _s["buf"]:
                    _s["last"] = time.time()
                    text = "\n".join(_s["buf"])
                    _s["buf"] = []
                    self._spawn(plugin.push(f"…{task.short} 运行中：\n{text}", task.umo))

            result = inj[1] if inj else await self._exec(
                task, spec, "agent", argv,
                timeout=spec.timeout or default_timeout,
                stdin_text=stdin_text, cancel=cancel, on_output=on_output)
            task.updated_at = time.time()

            if result.cancelled:  # /pc stop、/pc done 已经处理好状态
                self.save()
                return

            # ⓪ 远程节点被断电 / 崩溃打断：明确告诉主人，按策略续跑或等回话，绝不当成正常结束
            if result.extra.get("interrupted"):
                task.session_started = True
                task.last_error = result.error
                if result.output.strip():
                    task.last_output = result.output
                auto = str(plugin.cfg("node_interrupted_policy", "ask")) == "resume"
                interrupts += 1
                if auto and interrupts <= 3:
                    task.instruction = ("上一次执行被意外中断（电脑断电或客户端崩溃），"
                                        f"请先检查项目当前状态，然后继续完成：{task.goal}")
                    self.save()
                    await plugin.push(f"⚡ {task.short} 这一轮在节点上被中断：{result.error}\n"
                                      f"按配置自动续跑（第 {interrupts} 次）。", task.umo)
                    continue
                task.status = TaskStatus.WAITING
                self.save()
                await plugin.push(
                    f"⚡ {task.short} 这一轮在节点 {spec.node} 上被中断，没有正常结束：\n{result.error}\n"
                    f"接着跑：/pc go {task.short}；看已回传的输出：/pc log {task.short}", task.umo)
                return

            # ① 根本没起来（找不到命令、参数不安全等）
            if not result.launched:
                task.status = TaskStatus.FAILED
                task.last_error = result.error
                self.save()
                await plugin.push(f"❌ {task.software} 起不来：{result.error}", task.umo)
                return

            task.session_started = True
            sid = spec.find_session(result.output)
            if sid:
                task.session_id = sid

            # ② 撞额度：这一轮不算数，原指令留着，到点再跑
            hit, resume_at, guessed = limits.detect(
                result.tail_text or result.output,
                fallback_minutes=safe_int(plugin.cfg("quota_default_wait_minutes", 60), 60, 1),
                exit_code=result.exit_code,
                extra_patterns=spec.quota_patterns,
            )
            if hit:
                task.status = TaskStatus.QUOTA
                task.resume_at = resume_at or 0.0
                task.last_output = result.output
                if not task.instruction.startswith(QUOTA_PREFIX):
                    task.instruction = QUOTA_PREFIX + task.instruction
                self.save()
                when = human_time(task.resume_at) + ("（没读出恢复时间，先按默认等）" if guessed else "")
                await plugin.push(
                    f"⏳ {task.software} 额度用完了（{task.short}）。\n"
                    f"预计 {when} 恢复，到点我自动接着跑。\n"
                    f"不想等：/pc stop {task.short}；想立刻重试：/pc go {task.short}\n"
                    f"—— 最后几行 ——\n{preview_lines(result.tail_text, 6)}", task.umo)
                return

            if task.instruction.startswith(QUOTA_PREFIX):
                task.instruction = task.instruction[len(QUOTA_PREFIX):]
            task.rounds = round_no
            task.last_output = result.output
            task.last_error = "" if result.ok else (result.error or f"退出码 {result.exit_code}")
            task.history.append({
                "round": round_no,
                "instruction": preview(task.instruction, 200),
                "ok": result.ok,
                "exit_code": result.exit_code,
                "seconds": int(result.duration),
                "at": time.time(),
            })
            del task.history[:-30]

            # ③ 推送这一轮的输出
            head = (f"{'✅' if result.ok else '⚠️'} {task.short} · {task.software} 第{round_no}轮结束"
                    f" · 用时 {human_duration(result.duration)} · 退出码 {result.exit_code}"
                    + ("（超时被掐）" if result.timed_out else ""))
            if spec.verify and not result.timed_out:
                head += " · 接下来验收"
            await plugin.push(f"{head}\n{plugin.describe_output(result)}", task.umo)

            if result.timed_out:
                task.status = TaskStatus.WAITING
                self.save()
                await plugin.push(
                    f"⏰ {task.short} 这一轮超时（{human_duration(spec.timeout or default_timeout)}）被掐掉了。"
                    f"要接着做：/pc go {task.short} [指令]；可以调大 timeout。", task.umo)
                return

            # ④ 验收关卡，⑤⑥ 自主续轮 / 等主人回话
            step = await self._verify_gate(task, spec, cancel)
            if step == "retry":
                continue
            if step == "stop":
                return
            if await self._after_round(task, spec, started, sentinel) == "continue":
                continue
            return

    async def _verify_gate(self, task: Task, spec: SoftwareSpec, cancel: asyncio.Event,
                           v_res=None) -> str:
        """验收关卡。返回 pass（通过 / 没配验收）、retry（带着失败日志再修一轮）、stop（停下）。"""
        plugin = self.plugin
        if not spec.verify:
            return "pass"
        if v_res is None:
            v_res = await self._run_verify(task, spec, cancel)
        if v_res.cancelled:
            self.save()
            return "stop"
        # 验收命令根本没跑起来（命令名写错、节点不对）或在节点上被断电打断：
        # 这不是“代码没过验收”，不计失败次数，更不能把报错喂给 AI 去改没坏的代码
        if v_res.extra.get("interrupted") or not v_res.launched:
            task.status = TaskStatus.WAITING
            task.last_error = v_res.error
            self.save()
            if v_res.extra.get("interrupted"):
                what = "在节点上被中断（断电 / 崩溃）"
            else:
                # 不一律说“没能启动”：被节点按 allowed_roots 拒绝时进程是起了的，
                # 提示得指向真正的原因（下面会把 v_res.error 原样带出来）
                what = "没能正常执行（命令名写错、节点没连上，或工作目录被节点拒绝）"
            await plugin.push(
                f"⚠️ {task.short} 的验收命令{what}：{v_res.error}\n"
                f"这不算验收失败，不占自动修复次数，也不会让 AI 去改代码。"
                f"检查 verify 配置 / 节点后 /pc go {task.short}。", task.umo)
            return "stop"
        task.last_verify = v_res.tail_text or v_res.output or v_res.error
        task.verify_ok = v_res.ok
        task.updated_at = time.time()
        if task.history:
            task.history[-1]["verify"] = v_res.ok
        if v_res.ok:
            task.verify_attempts = 0
            self.save()
            await plugin.push(
                f"✅ 验收通过（{preview(spec.verify_text(), 60)}"
                f" · 用时 {human_duration(v_res.duration)}）", task.umo)
        else:
            task.verify_attempts += 1
            max_v = safe_int(plugin.cfg("verify_max_attempts", 3), 3, 0)
            self.save()
            why = "超时" if v_res.timed_out else f"退出码 {v_res.exit_code}"
            await plugin.push(
                f"❌ 验收没过（第{task.verify_attempts}次 · {why}"
                f" · 用时 {human_duration(v_res.duration)}）\n"
                f"{plugin.describe_output(v_res, compact=True)}", task.umo)
            if task.verify_attempts <= max_v:
                task.instruction = (
                    f"刚才改完之后，验收命令 `{spec.verify_text()}` 没有通过。"
                    f"整个任务的目标是：{task.goal}\n\n"
                    f"验收命令输出的末尾：\n{(task.last_verify or '')[-2500:]}\n\n"
                    f"请根据这份失败信息继续修，只改必要的地方，不要推翻重做；"
                    f"不要为了让验收通过去删除或跳过测试。")
                self.save()
                await plugin.push(
                    f"♻️ 自己再修一轮（自动修复第 {task.verify_attempts}/{max_v} 次）",
                    task.umo)
                return "retry"
            task.status = TaskStatus.WAITING
            self.save()
            await plugin.push(
                (f"⚠️ 验收连续 {task.verify_attempts} 次没过，自动修复次数（{max_v}）用完了，先停下。"
                 if max_v else "⚠️ 验收没过（自动修复已关闭）。")
                + f"\n想换思路直接回我一句，或 /pc go {task.short} <新指令>。", task.umo)
            return "stop"
        return "pass"

    async def _after_round(self, task: Task, spec: SoftwareSpec, started: float, sentinel: str) -> str:
        """一轮（含验收）结束后：自主模式拟下一步（continue），否则停下等主人（stop）。"""
        plugin = self.plugin
        if task.autopilot:
            why = self._autopilot_budget_left(task, started)
            if why:
                task.status = TaskStatus.WAITING
                self.save()
                await plugin.push(
                    f"🏁 {task.short} 自主模式先停：{why}。\n"
                    f"要接着干：/pc go {task.short} [补充指令]；不管了：/pc done {task.short}",
                    task.umo)
                return "stop"
            nxt = await self._ask_next(task, sentinel, spec)
            if nxt is None:
                task.status = TaskStatus.WAITING
                self.save()
                await plugin.push(
                    f"⚠️ 自主模式要靠 AstrBot 当前的模型拟下一步，但模型没回应，先停下等你。\n"
                    f"/pc go {task.short} <指令> 手动接着跑。", task.umo)
                return "stop"
            if sentinel in nxt:
                task.status = TaskStatus.DONE
                self.save()
                await plugin.push(
                    f"🏁 我判断 {task.short} 的目标已经达成，收工。"
                    f"{'（验收也是绿的）' if spec.verify and task.verify_ok else ''}\n"
                    f"还有要求就 /pc go {task.short} <指令>。", task.umo)
                return "stop"
            task.instruction = nxt
            self.save()
            await plugin.push(f"🤖 {task.short} 下一轮我自己定的指令：\n{nxt}", task.umo)
            return "continue"

        # ⑥ 普通模式：等主人回话
        task.status = TaskStatus.WAITING
        self.save()
        await plugin.push(
            f"👉 {task.short} 接下来怎么办？直接回我一句就是给它的新指令；"
            f"/pc go {task.short} 按默认“继续”；/pc done {task.short} 收工。", task.umo)
        return "stop"

    async def _ask_next(self, task: Task, sentinel: str, spec: SoftwareSpec) -> str | None:
        """让模型根据目标 + 上一轮输出决定下一条指令。模型调用失败返回 None。"""
        verify_part = ""
        if spec.verify:
            verify_part = (f"验收命令 `{spec.verify_text()}` 的结果："
                           f"{'通过' if task.verify_ok else '没通过'}。\n")
        prompt = (
            "你是一个远程调度器，正在替主人指挥一个命令行编程 AI 连续干活。\n"
            f"长期目标：{task.goal}\n"
            f"已经跑了 {task.rounds} 轮。\n"
            f"上一轮给它的指令：{preview(task.instruction, 300)}\n"
            f"{verify_part}"
            f"上一轮的输出（末尾）：\n{(task.last_output or '')[-3000:]}\n\n"
            "请判断：目标是不是已经完成，或者已经没法继续了（缺信息、需要主人决定、反复失败）？\n"
            f"- 是：只输出 {sentinel}，不要写别的字。\n"
            "- 否：只输出下一条要交给它的指令（中文，具体可执行，不要解释、不要编号、不要寒暄）。"
        )
        text = await self.plugin.llm_text(task.umo, prompt)
        if not text:
            return None
        text = text.strip().strip("`").strip()
        return text[:1500] if text else None

    # ---------------- 操作 ----------------
    def stop(self, task: Task, status: str = TaskStatus.STOPPED) -> None:
        """停下任务（正在跑的会连进程树一起杀掉）。"""
        task.status = status
        task.updated_at = time.time()
        task.resume_at = 0.0
        ev = self._cancel.get(task.id)
        if ev:
            ev.set()  # 正在跑子进程的话，runner 会杀掉整个进程树
        rt = self._runners.get(task.id)
        if rt and not rt.done():
            rt.cancel()  # 不在跑子进程（比如在等模型拟指令）也要停下
        self.save()

    def reply(self, task: Task, text: str) -> Task | None:
        """把主人这句话当成新指令接着跑。返回挡路任务（排队时）。"""
        text = (text or "").strip()
        if text:
            task.instruction = text
            if not task.autopilot:
                task.goal = text  # 普通模式下，主人新给的指令就是当前目标
        elif not task.instruction.startswith(QUOTA_PREFIX):
            task.instruction = "继续，接着上文往下做。"  # 额度中断的原指令则照旧
        task.verify_ok = False
        task.verify_attempts = 0
        task.round_base = task.rounds
        task.resume_at = 0.0
        task.notified_restart = True
        if task.status == TaskStatus.PENDING:
            self.save()
            return self.busy_with(task)
        return self.submit(task)

    def resume_with(self, task: Task, kind: str, result) -> Task | None:
        """把插件重启期间节点跑完的结果（agent / verify）接回任务的正常流程。"""
        self._injected[task.id] = (kind, result)
        task.notified_restart = True
        return self.submit(task)

    def get(self, key: str) -> Task | None:
        """按完整 id 或前缀找任务；前缀对上多个时取最新的。"""
        key = (key or "").strip().lower()
        if not key:
            return None
        if key in self.tasks:
            return self.tasks[key]
        if len(key) < 4:  # 太短的前缀容易和普通词撞上
            return None
        hits = [t for t in self.tasks.values() if t.id.startswith(key)]
        return max(hits, key=lambda t: t.created_at) if hits else None

    def latest(self, umo: str = "", statuses=None) -> Task | None:
        """当前会话里最近的一条任务（可限定状态）。"""
        items = [t for t in self.tasks.values()
                 if (not umo or t.umo == umo) and (statuses is None or t.status in statuses)]
        return max(items, key=lambda t: t.updated_at or t.created_at) if items else None

    def waiting_tasks(self, umo: str) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == TaskStatus.WAITING and t.umo == umo]

    def clear_finished(self) -> int:
        gone = [tid for tid, t in self.tasks.items() if not t.is_active()]
        for tid in gone:
            self.tasks.pop(tid, None)
        self.save()
        return len(gone)

    async def watch(self) -> None:
        """后台巡检：额度恢复了就续跑；排队的放出去；重启打断的告诉主人。"""
        now = time.time()
        for task in list(self.tasks.values()):
            if task.status == TaskStatus.QUOTA and (not task.resume_at or task.resume_at <= now):
                task.resume_at = 0.0
                blocker = self.submit(task)
                await self.plugin.push(
                    f"♻️ {task.software} 的额度应该恢复了，{task.short} "
                    + (f"排在 {blocker.short} 后面，轮到就跑。" if blocker else "接着跑。"),
                    task.umo)
            elif task.status == TaskStatus.WAITING and not task.notified_restart:
                task.notified_restart = True
                self.save()
                if bool(self.plugin.cfg("resume_interrupted", False)):
                    self.reply(task, "")
                    await self.plugin.push(
                        f"♻️ {task.short} 上次被 AstrBot 重启打断了，按配置自动接着跑。", task.umo)
                else:
                    await self.plugin.push(
                        f"⚠️ {task.short}（{task.software}）上次跑到一半被 AstrBot 重启打断了。\n"
                        f"接着跑：/pc go {task.short}；不要了：/pc done {task.short}", task.umo)
        self.kick_queue()

    def snapshot(self, include_done: bool = False) -> str:
        items = [t for t in self.tasks.values() if include_done or t.is_active()]
        items.sort(key=lambda t: t.created_at)
        if not items:
            return "现在没有进行中的任务。" + ("" if include_done else "（/pc ls all 看全部记录）")
        lines = []
        for t in items[-20:]:
            extra = ""
            if t.status == TaskStatus.QUOTA and t.resume_at:
                extra = f" · {human_time(t.resume_at)} 恢复"
            if t.verify_attempts:
                extra += f" · ❌验收×{t.verify_attempts}"
            elif t.verify_ok:
                extra += " · ✅已验收"
            where = f" @{t.project}" if t.project else ""
            lines.append(
                f"· {t.short} [{STATUS_TEXT.get(t.status, t.status)}] {t.software}{where}"
                f" 第{t.rounds}轮{' · 自主' if t.autopilot else ''}{extra}\n  {preview(t.goal, 80)}")
        return "\n".join(lines)


def preview_lines(text: str, n: int) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:]) or "（没有输出）"
