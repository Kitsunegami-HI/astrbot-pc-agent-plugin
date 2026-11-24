"""远程电脑代理：通过 QQ 调度本机的命令行 AI，跑完推结果，额度用完自动等，验收不过自动修。"""
from __future__ import annotations

import asyncio
import os
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.node import NodeHub
from .core.runner import ProcessRunner, RunResult
from .core.software import normalize_projects, normalize_software, resolve_workdir
from .core.task import STATUS_TEXT, TaskManager, TaskStatus
from .core.utils import chunk_text, human_time, preview, safe_int, split_first

PLUGIN_NAME = "astrbot_plugin_pc_agent"
VERSION = "0.3.5"
CMD_WORDS = ("pc", "电脑", "远程")
TOOL_NAME = "remote_pc_agent"

HELP_TEXT = """🐾 远程电脑代理 · 指令一览

/pc run <软件>[@项目] <指令>   开一个任务（跑完主动推给你）
/pc auto <软件>[@项目] <指令>  自主模式：自己决定下一步，连着干到目标完成
/pc ls [all]          看进行中的任务（all = 连已结束的一起）
/pc log [编号]        把某个任务最近的输出再发一遍（不写编号 = 最近那条）
/pc go [编号] [指令]  接着跑；不给指令就按默认“继续”
/pc stop [编号]       停下（连子进程一起掐）
/pc done [编号]       这条任务收工，别再接管我的话
/pc sw                白名单软件和项目目录
/pc node              远程节点在线状态、补发队列
/pc clear             清掉已结束的任务记录
/pc reload            改完插件配置后重新读一遍
/pc help              看这一页

只有一个软件时可以省略软件名：/pc run 把报错修了
编号是 /pc ls 里的六位短号，输前几位也能认出来。
任务在“等你回话”时，你在同一个会话里直接说话，就当成给它的新指令；
多个任务同时在等时请用 /pc go <编号> 指明。"""


@register(PLUGIN_NAME, "无所谓～～ & 琴葉",
          "通过 QQ 远程调度本机的命令行 AI（Codex / Claude Code 等）", VERSION)
class PCAgentPlugin(Star):
    """插件主类：指令、鉴权、主动推送、后台巡检。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._data_dir = self._resolve_data_dir()
        self.specs = {}
        self.projects = {}
        self.runner: ProcessRunner | None = None
        self.reload_config()
        self.tasks = TaskManager(self)
        self.tasks.load()
        self.node_hub = NodeHub(self)
        self._hub_started = False

        self._shutdown = asyncio.Event()
        self._watch_task: asyncio.Task | None = None
        self._ensure_watch()
        logger.info(f"[pc_agent] v{VERSION} 已加载，白名单 {list(self.specs) or '空'}")

    async def initialize(self):
        """AstrBot 加载完插件后调用：启动巡检，按配置开关 LLM 工具。"""
        self._ensure_watch()
        self._sync_llm_tool()

    # ================= 基础 =================
    def cfg(self, key: str, default=None):
        try:
            val = self.config.get(key, default)
        except Exception:
            return default
        return default if val is None else val

    def reload_config(self) -> None:
        self.specs = normalize_software(self.cfg("software", {}))
        self.projects = normalize_projects(self.cfg("projects", {}))
        self.runner = ProcessRunner(
            tail_lines=safe_int(self.cfg("output_tail_lines", 200), 200, 50),
            head_lines=safe_int(self.cfg("push_head_lines", 20), 20, 0))

    @staticmethod
    def _resolve_data_dir() -> str:
        try:
            from astrbot.api.star import StarTools
            path = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            path = os.path.join("data", "plugin_data", PLUGIN_NAME)
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def data_dir(self) -> str:
        return self._data_dir

    def _ensure_watch(self) -> None:
        """巡检协程没起来就起一个（构造时可能还没有事件循环）。"""
        if self._watch_task and not self._watch_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watch_task = loop.create_task(self._watch_loop())
        if not self._hub_started:
            self._hub_started = True
            loop.create_task(self._start_hub())

    async def _start_hub(self) -> None:
        try:
            err = await self.node_hub.start()
        except Exception as exc:
            err = str(exc)
        if err:
            logger.error(f"[pc_agent] 远程节点监听没起来：{err}")

    async def on_orphan_result(self, meta: dict, result, job_id: str = "") -> None:
        """插件重启期间节点跑完的那一轮：是当前这一轮就接回正常流程（额度识别、验收照常），过期的只存档。"""
        task = self.tasks.tasks.get(str(meta.get("task_id") or ""))
        if task is None:
            return
        kind = "verify" if meta.get("kind") == "verify" else "agent"
        label = "验收" if kind == "verify" else "执行"
        try:
            rnd = int(job_id.split("-")[1])
        except (IndexError, ValueError):
            rnd = -1
        if (rnd == task.rounds + 1 and task.status == TaskStatus.WAITING
                and task.software in self.specs):
            blocker = self.tasks.resume_with(task, kind, result)
            await self.push(
                f"📬 {task.short}：插件重启期间节点把这一轮{label}跑完了（退出码 {result.exit_code}），"
                f"结果接回正常流程，额度识别和验收照常进行"
                + (f"（排在 {blocker.short} 后面）。" if blocker else "。"), task.umo)
            return
        if kind == "agent" and result.output.strip():
            task.last_output = result.output
        elif result.output.strip():
            task.last_verify = result.output
        self.tasks.save()
        await self.push(
            f"📬 {task.short} 收到一条过期的节点{label}结果（退出码 {result.exit_code}），任务已经往前走了，"
            f"只存进记录：/pc log {task.short}", task.umo)

    def _sync_llm_tool(self) -> None:
        """LLM 工具默认关闭；关闭时从模型可用的工具列表里撤掉。"""
        enabled = bool(self.cfg("enable_llm_tool", False))
        fn = getattr(self.context, "activate_llm_tool" if enabled else "deactivate_llm_tool", None)
        if callable(fn):
            try:
                fn(TOOL_NAME)
            except Exception as exc:
                logger.debug(f"[pc_agent] 切换 LLM 工具状态失败：{exc}")

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """只有主人能用：配了 admin_qq 就只认这些号，否则用 AstrBot 的管理员判定。"""
        allow = self.cfg("admin_qq", []) or []
        if isinstance(allow, (str, int)):
            allow = [x.strip() for x in str(allow).replace("，", ",").split(",") if x.strip()]
        allow = [str(x).strip() for x in allow if str(x).strip()]
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = ""
        if allow:
            return bool(sender) and sender in allow
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return bool(sender) and sender in [str(x) for x in admins]
        except Exception:
            return False

    # ================= 推送 =================
    @staticmethod
    def _chain(text: str):
        try:
            from astrbot.api.event import MessageChain
        except Exception:  # 老版本
            from astrbot.core.message.message_event_result import MessageChain  # type: ignore
        try:
            return MessageChain().message(text)
        except Exception:
            from astrbot.api.message_components import Plain
            return MessageChain([Plain(text)])

    async def push(self, text: str, umo: str = "") -> None:
        """主动推一条消息；太长就自动分段，段数有上限。"""
        target = umo or str(self.cfg("notify_session", "") or "") or self.tasks.last_umo
        if not target:
            logger.warning(f"[pc_agent] 不知道该往哪个会话推送：{preview(text, 200)}")
            return
        limit = safe_int(self.cfg("push_chunk_chars", 1200), 1200, 200)
        parts = chunk_text(text, limit)
        max_parts = safe_int(self.cfg("push_max_messages", 4), 4, 1)
        if len(parts) > max_parts:
            parts = parts[:max_parts - 1] + [
                f"……还有 {len(parts) - max_parts + 1} 段没发，完整的看 /pc log"]
        for part in parts:
            try:
                await self.context.send_message(target, self._chain(part))
            except Exception:
                logger.exception("[pc_agent] 推送失败")
                return
            await asyncio.sleep(0.4)

    def describe_output(self, result: RunResult, compact: bool = False) -> str:
        """把输出裁成“开头 + 省略 + 结尾”，QQ 里看得下去。"""
        text = (result.output or "").strip()
        if result.error:
            text = (text + "\n" + result.error).strip()
        if not text:
            return "（没有输出）"
        lines = text.split("\n")
        head_n = 0 if compact else safe_int(self.cfg("push_head_lines", 20), 20, 0)
        tail_n = safe_int(self.cfg("push_tail_lines", 40), 40, 5)
        if compact:
            tail_n = min(tail_n, 25)
        if len(lines) > head_n + tail_n + 3:
            omitted = len(lines) - head_n - tail_n
            lines = (lines[:head_n] if head_n else []) + \
                [f"……（省略 {omitted} 行，/pc log 看更多）……"] + lines[-tail_n:]
        text = "\n".join(lines)
        max_chars = safe_int(self.cfg("push_max_chars", 3000), 3000, 300)
        if len(text) > max_chars:  # 每行都很长时再按字数裁一次，结尾更重要
            keep_head = max_chars // 4 if head_n else 0
            text = (text[:keep_head] + "\n……（太长了，中间省略）……\n"
                    + text[-(max_chars - keep_head):])
        return text

    async def llm_text(self, umo: str, prompt: str) -> str:
        """借 AstrBot 当前的模型算一段文本；失败返回空串。"""
        target = umo or self.tasks.last_umo
        try:
            if hasattr(self.context, "llm_generate"):
                provider_id = await self.context.get_current_chat_provider_id(umo=target)
                resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            else:  # 4.5 之前的老版本
                provider = self.context.get_using_provider(umo=target)
                if provider is None:
                    return ""
                resp = await provider.text_chat(prompt=prompt, contexts=[])
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[pc_agent] 调用模型失败：{exc}")
            return ""

    # ================= 起任务 =================
    def _pick_software(self, rest: str) -> tuple[str, str, str, bool]:
        """从「codex@blog 修 bug」里拆出 (软件, 项目, 指令, 是否兜底)。省略软件名时用唯一的那个。"""
        first, tail = split_first(rest)
        name, at, project = first.partition("@")
        if name.lower() in self.specs or at:
            # 写了 @项目 就一定按“软件@项目”解析：软件不在白名单、项目没配置，start_task 都会明确报错，
            # 绝不把整句当成指令、悄悄丢掉 @项目 跑到默认目录里
            return name.lower(), project.lower(), tail, False
        if len(self.specs) == 1:
            # 白名单只有一个软件时，整句都当指令（“把报错修了”不该被当成软件名）。
            # 代价是软件名写错时也会落到这个唯一软件上，所以回执里会注明是按唯一软件处理的。
            return next(iter(self.specs)), "", rest, True
        return name.lower(), project.lower(), tail, False

    async def start_task(self, software: str, instruction: str, umo: str = "",
                         autopilot: bool = False, project: str = "", via: str = "cmd",
                         via_unique: bool = False) -> str:
        """建任务并开跑，返回一句给主人看的回执。"""
        self._ensure_watch()
        name = (software or "").strip().lower()
        if not name and len(self.specs) == 1:
            name = next(iter(self.specs))
        spec = self.specs.get(name)
        if spec is None:
            return (f"白名单里没有「{name or '空'}」。可选："
                    f"{'、'.join(self.specs) if self.specs else '一个都没配'}"
                    f"（在插件配置的 software 里加）")
        if not (instruction or "").strip():
            return "指令是空的，说点具体要它做的事。"
        override = ""
        if project:
            override = self.projects.get(project.lower(), "")
            if not override:
                return (f"没有叫「{project}」的项目。已配置的项目："
                        f"{'、'.join(self.projects) or '（没有，去插件配置的 projects 里加）'}")
        workdir, err = resolve_workdir(spec, str(self.cfg("working_dir", "") or ""), override)
        if err:
            return f"没开跑：{err}"

        task = self.tasks.new(name, instruction, umo=umo, autopilot=autopilot,
                              workdir=workdir, project=project.lower(), node=spec.node)
        blocker = self.tasks.submit(task)
        mode = "自主模式" if autopilot else "普通模式"
        note = (f"\n（没写软件名，按白名单里唯一的「{name}」处理；要用别的软件先在配置里加上）"
                if via_unique else "")
        if blocker is not None:
            return (f"收到，{task.short} 已排队 · {name} · {mode}{note}\n"
                    f"同一个目录里 {blocker.short} 还在跑，等它这一轮结束就轮到你。")
        msg = f"收到，{task.short} 开始跑了 · {name} · {mode}{note}"
        if via == "cmd":
            msg += f"\n工作目录：{workdir}\n跑完我把输出发你。"
        if spec.node:
            msg += (f"\n执行节点：{spec.node}（{'在线' if self.node_hub.is_online(spec.node) else '离线，连上后自动派过去'}）")
        return msg

    # ================= 指令 =================
    @staticmethod
    def _split(event: AstrMessageEvent) -> tuple[str, str]:
        """「pc run codex xxx」→ ("run", "codex xxx")；指令正文里的换行原样保留。"""
        raw = (event.message_str or "").strip().lstrip("/").strip()
        first, rest = split_first(raw)
        if first.lower() in CMD_WORDS:
            raw = rest
        sub, rest = split_first(raw)
        return sub.lower(), rest

    def _find(self, key: str, umo: str, statuses=None):
        """按编号找任务；没写编号就取当前会话最近的一条。"""
        if key:
            return self.tasks.get(key)
        return self.tasks.latest(umo, statuses) or self.tasks.latest("", statuses)

    @filter.command("pc", alias={"电脑", "远程"})
    async def pc(self, event: AstrMessageEvent):
        """远程电脑代理：/pc help 看用法"""
        if not self.is_admin(event):
            yield event.plain_result("这个只有主人能用哦。")
            return
        self._ensure_watch()
        sub, rest = self._split(event)
        umo = event.unified_msg_origin
        key, extra = split_first(rest)

        if sub in ("", "help", "?", "帮助"):
            yield event.plain_result(HELP_TEXT)
            return

        if sub in ("ls", "list", "列表"):
            yield event.plain_result(self.tasks.snapshot(include_done=key.lower() in ("all", "全部")))
            return

        if sub in ("sw", "软件", "whitelist"):
            yield event.plain_result(self._describe_whitelist())
            return

        if sub in ("reload", "重载"):
            self.reload_config()
            self._sync_llm_tool()
            yield event.plain_result(f"配置读完了，白名单：{'、'.join(self.specs) or '空'}；"
                                     f"项目：{'、'.join(self.projects) or '无'}")
            return

        if sub in ("node", "nodes", "节点"):
            yield event.plain_result(self.node_hub.describe())
            return

        if sub in ("clear", "清理"):
            n = self.tasks.clear_finished()
            yield event.plain_result(f"清掉了 {n} 条已结束的任务记录。")
            return

        if sub in ("run", "跑", "auto", "自主"):
            autopilot = sub in ("auto", "自主")
            if not rest:
                yield event.plain_result(
                    f"用法：/pc {'auto' if autopilot else 'run'} <软件>[@项目] <指令>\n"
                    f"可用软件：{'、'.join(self.specs) or '（还没配）'}")
                return
            software, project, instruction, via_unique = self._pick_software(rest)
            yield event.plain_result(await self.start_task(
                software, instruction, umo=umo, autopilot=autopilot, project=project,
                via_unique=via_unique))
            return

        if sub not in ("log", "日志", "go", "继续", "reply", "stop", "停", "kill",
                       "done", "完成", "收工"):
            yield event.plain_result(f"没看懂「{sub}」。\n\n{HELP_TEXT}")
            return

        # 编号可省略：第一个词不像编号时，整段都当成指令
        task = self.tasks.get(key) if key else None
        if task is None and re.fullmatch(r"[0-9a-f]{4,8}", key.lower()) and re.search(r"\d", key):
            yield event.plain_result(f"找不到编号 {key} 的任务，先 /pc ls 看看。")
            return
        if task is None:
            extra = rest
            if sub in ("go", "继续", "reply"):
                task = self._find("", umo, (TaskStatus.WAITING, TaskStatus.QUOTA,
                                             TaskStatus.STOPPED, TaskStatus.FAILED))
            elif sub in ("stop", "停", "kill"):
                task = self._find("", umo, (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.QUOTA))
            elif sub in ("done", "完成", "收工"):
                task = self._find("", umo, (TaskStatus.WAITING, TaskStatus.QUOTA,
                                             TaskStatus.RUNNING, TaskStatus.PENDING))
            else:
                task = self._find("", umo)
        if task is None:
            yield event.plain_result("没找到对应的任务，先 /pc ls 看看编号。")
            return

        if sub in ("log", "日志"):
            yield event.plain_result(self._describe_log(task))
            return

        if sub in ("go", "继续", "reply"):
            if task.status == TaskStatus.RUNNING:
                yield event.plain_result(f"{task.short} 还在跑，等它这一轮结束。")
                return
            if task.software not in self.specs:
                yield event.plain_result(f"{task.short} 用的「{task.software}」已经不在白名单里了。")
                return
            blocker = self.tasks.reply(task, extra)
            tip = f"（排在 {blocker.short} 后面）" if blocker else ""
            yield event.plain_result(
                f"好，{task.short} 接着跑{tip}：{preview(task.instruction, 60)}")
            return

        if sub in ("stop", "停", "kill"):
            if not task.is_active():
                yield event.plain_result(f"{task.short} 本来就没在进行（{STATUS_TEXT.get(task.status)}）。")
                return
            self.tasks.stop(task)
            yield event.plain_result(f"{task.short} 停了。想再接着跑：/pc go {task.short}")
            return

        # done
        self.tasks.stop(task, status=TaskStatus.DONE)
        yield event.plain_result(f"{task.short} 收工。以后普通聊天我不会再拦。")

    def _describe_whitelist(self) -> str:
        if not self.specs:
            return "白名单是空的，去插件配置的 software 里加一个。"
        default_dir = str(self.cfg("working_dir", "") or "")
        lines = ["白名单："]
        for s in self.specs.values():
            where, err = resolve_workdir(s, default_dir)
            check = f"\n    验收：{preview(s.verify_text(), 50)}" if s.verify else ""
            node = ""
            if s.node:
                node = f" · 节点 {s.node}（{'🟢在线' if self.node_hub.is_online(s.node) else '🔴离线'}）"
            lines.append(f"· {s.name} → {where or '⚠️ ' + err}{node}{check}")
        if self.projects:
            lines.append("项目（/pc run <软件>@<项目> 用）：")
            for name, path in self.projects.items():
                ok = "" if os.path.isdir(os.path.expanduser(path)) else "（⚠️ 目录不存在）"
                lines.append(f"· {name} → {path}{ok}")
        lines.append(f"LLM 工具：{'开' if self.cfg('enable_llm_tool', False) else '关'}"
                     f" · 被动接话：{'开' if self.cfg('passive_reply', True) else '关'}")
        return "\n".join(lines)

    def _describe_log(self, task) -> str:
        head = (f"{task.short} · {task.software}{' @' + task.project if task.project else ''}"
                f" · 第{task.rounds}轮 · {STATUS_TEXT.get(task.status, task.status)}")
        if task.status == TaskStatus.QUOTA and task.resume_at:
            head += f" · {human_time(task.resume_at)} 恢复"
        parts = [head, f"目标：{preview(task.goal, 120)}"]
        if task.last_error:
            parts.append(f"最近的问题：{preview(task.last_error, 200)}")
        body = (task.last_output or "").strip()
        parts.append(self.describe_output(RunResult(ok=True, exit_code=0, output=body))
                     if body else "（还没有输出）")
        if task.last_verify and not task.verify_ok:
            parts.append("—— 最近一次验收输出 ——\n" + self.describe_output(
                RunResult(ok=False, exit_code=1, output=task.last_verify), compact=True))
        return "\n".join(parts)

    # ================= 被动接管 =================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """任务在等回话时，主人在同一个会话里直接说话，就当成给它的下一条指令。"""
        if not bool(self.cfg("passive_reply", True)):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        # AstrBot 会先把唤醒前缀“/”剥掉再交给插件，所以要自己认出 /pc 指令，免得同一句话被处理两次
        raw = str(getattr(getattr(event, "message_obj", None), "message_str", "") or "").strip()
        first, _ = split_first(text.lstrip("/"))
        if raw.startswith("/") or text.startswith("/") or first.lower() in CMD_WORDS:
            return
        # 群聊里必须 @机器人 或带唤醒前缀，免得主人在群里随口一句被当成指令
        try:
            private = event.is_private_chat()
        except Exception:
            private = False
        if not private and not getattr(event, "is_at_or_wake_command", False):
            return
        if not self.is_admin(event):
            return
        waiting = self.tasks.waiting_tasks(event.unified_msg_origin)
        if not waiting:
            return
        try:
            event.stop_event()  # 别让这条消息再进普通对话
        except Exception:
            pass
        if len(waiting) > 1:
            ids = "、".join(t.short for t in waiting)
            yield event.plain_result(
                f"现在有 {len(waiting)} 个任务都在等你回话（{ids}），我不确定这句给谁。\n"
                f"请用 /pc go <编号> {preview(text, 30)}")
            return
        task = waiting[0]
        blocker = self.tasks.reply(task, text)
        tip = f"（排在 {blocker.short} 后面）" if blocker else ""
        yield event.plain_result(
            f"记下了，{task.short} 按这句继续{tip}：{preview(text, 60)}\n"
            f"（不想让它接管聊天，用 /pc done {task.short}）")

    # ================= LLM 工具 =================
    @filter.llm_tool(name=TOOL_NAME)
    async def remote_pc_agent(self, event: AstrMessageEvent, software: str,
                              instruction: str, autopilot: bool = False):
        """在主人自己的电脑上后台运行一个命令行编程 AI（如 codex / claude），跑完后结果会自动推送给主人。适合“帮我把这个报错修了”“跑一下测试”这类需要动电脑的请求。只有主人本人提出时才能调用。

        Args:
            software(string): 要用的软件名，必须是白名单里的（如 codex、claude）；可以写成 软件@项目 指定项目目录
            instruction(string): 交给它执行的指令，一句具体可执行的中文，不要包含闲聊
            autopilot(boolean): 是否让它自己连续多轮推进到目标完成
        """
        if not bool(self.cfg("enable_llm_tool", False)):
            return "远程电脑代理的 LLM 工具没有开启（插件配置 enable_llm_tool），请让主人用 /pc run 指令。"
        if not self.is_admin(event):
            return "只有主人本人可以让我动电脑，这个请求已拒绝。"
        name, _, project = str(software or "").strip().partition("@")
        return await self.start_task(name, str(instruction or ""), umo=event.unified_msg_origin,
                                     autopilot=bool(autopilot), project=project, via="tool")

    # ================= 后台巡检 =================
    async def _watch_loop(self) -> None:
        """定期看：额度恢复了没、排队的能不能放、有没有被重启打断的要通知。"""
        await asyncio.sleep(3)  # 等平台适配器连上再推送
        while not self._shutdown.is_set():
            try:
                await self.tasks.watch()
                await self.node_hub.watch()
            except Exception:
                logger.exception("[pc_agent] 巡检出错")
            interval = safe_int(self.cfg("watch_interval_seconds", 30), 30, 5)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def terminate(self) -> None:
        """插件被卸载/重载时收尾：杀掉正在跑的进程，把被打断的任务记成待续。"""
        self._shutdown.set()
        self.tasks.shutting_down = True
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        runners = [rt for rt in self.tasks._runners.values() if not rt.done()]
        for ev in list(self.tasks._cancel.values()):
            ev.set()
        for rt in runners:
            rt.cancel()
        if runners:
            await asyncio.wait(runners, timeout=20)
        self.tasks.save()
        await self.node_hub.stop()
        logger.info("[pc_agent] 已卸载，任务状态已存盘")
