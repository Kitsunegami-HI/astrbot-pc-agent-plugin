"""软件白名单：把配置里的模板变成真正能执行的 argv。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .utils import safe_int


@dataclass
class SoftwareSpec:
    """一个被允许调用的命令行 AI。"""

    name: str
    cmd: list[str]
    resume: list[str] = field(default_factory=list)
    cwd: str = ""
    timeout: int = 0                 # 0 = 用全局 default_timeout
    prompt_via: str = "arg"          # arg / stdin
    env: dict = field(default_factory=dict)
    verify: list[str] = field(default_factory=list)   # 验收命令，比如 ["npm", "test"]
    verify_shell: bool = False       # True = 用系统 shell 跑验收命令（可以写 && 、管道）
    verify_timeout: int = 600
    session_pattern: str = ""        # 从输出里抓会话 id 的正则（第一个分组）
    quota_patterns: list[str] = field(default_factory=list)  # 额外的“额度用完”特征
    node: str = ""                   # 非空 = 派给这个远程节点（Windows 客户端）执行

    def build(self, prompt: str, resume: bool = False,
              session_id: str = "") -> tuple[list[str], str | None, bool]:
        """返回 (argv, 要写进 stdin 的文本或 None, 是否真的用了 resume 模板)。

        resume 模板里带 {session} 但还没抓到会话 id 时，退回 cmd 模板新开一个会话。
        """
        use_resume = bool(resume and self.resume)
        if use_resume and not session_id and any("{session}" in p for p in self.resume):
            use_resume = False
        template = list(self.resume if use_resume else self.cmd)
        if not template:
            raise ValueError(f"软件 {self.name} 没有配置 cmd")
        template = [p.replace("{session}", session_id) for p in template]
        if (self.prompt_via or "arg").lower() == "stdin":
            argv = [p.replace("{prompt}", "").strip() if "{prompt}" in p else p for p in template]
            return [p for p in argv if p], prompt, use_resume
        if not any("{prompt}" in p for p in template):
            template.append("{prompt}")  # 忘了写占位符就默认放最后
        return [p.replace("{prompt}", prompt) for p in template], None, use_resume

    def build_verify(self) -> list[str]:
        """验收命令的 argv。"""
        return [str(p) for p in self.verify if str(p).strip()]

    def verify_text(self) -> str:
        return " ".join(self.build_verify())

    def find_session(self, text: str) -> str:
        """按 session_pattern 从输出里抓会话 id，抓不到返回空串。"""
        if not self.session_pattern or not text:
            return ""
        try:
            found = re.findall(self.session_pattern, text, re.IGNORECASE | re.MULTILINE)
        except re.error:
            return ""
        if not found:
            return ""
        last = found[-1]
        if isinstance(last, tuple):
            last = next((x for x in last if x), "")
        return str(last).strip()


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path))) if path else ""


def resolve_workdir(spec: SoftwareSpec, default_dir: str = "",
                    override: str = "") -> tuple[str, str]:
    """决定在哪个目录里跑：指定的项目 > 软件自己的 cwd > 全局 working_dir。

    返回 (目录, 错误信息)。配了但目录不存在、或者一个都没配，都算错误——
    绝不悄悄退回 AstrBot 自己的目录去跑，免得 AI 改到机器人本体。
    """
    for label, cand in (("项目目录", override), (f"{spec.name} 的 cwd", spec.cwd),
                        ("working_dir", default_dir)):
        if not cand:
            continue
        if spec.node:  # 远程节点上的路径（如 D:\\code）服务端没法检查，原样交给客户端去校验
            return cand, ""
        path = _expand(cand)
        if os.path.isdir(path):
            return path, ""
        return "", f"{label}「{cand}」不存在，先在插件配置里改对。"
    return "", "还没配置工作目录：去插件配置里填 working_dir（或给这个软件单独配 cwd）。"


def _as_list(val) -> list[str]:
    if isinstance(val, str):
        return val.split()
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val if str(x) != ""]
    return []


def _parse_one(name: str, raw) -> SoftwareSpec | None:
    """解析一项配置；支持纯字符串命令和字典两种写法。"""
    name = str(name).strip()
    if not name:
        return None
    if isinstance(raw, str):
        parts = raw.split()
        return SoftwareSpec(name=name, cmd=parts) if parts else None
    if not isinstance(raw, dict):
        return None
    cmd = _as_list(raw.get("cmd"))
    if not cmd:
        return None
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    timeout = safe_int(raw.get("timeout"), 0, 0)
    via = str(raw.get("prompt_via") or "arg").strip().lower()
    return SoftwareSpec(
        name=name,
        cmd=cmd,
        resume=_as_list(raw.get("resume")),
        cwd=str(raw.get("cwd") or "").strip(),
        timeout=max(30, timeout) if timeout else 0,
        prompt_via=via if via in ("arg", "stdin") else "arg",
        env={str(k): str(v) for k, v in env.items()},
        verify=_as_list(raw.get("verify")),
        verify_shell=bool(raw.get("verify_shell", False)),
        verify_timeout=max(30, safe_int(raw.get("verify_timeout"), 600)),
        session_pattern=str(raw.get("session_pattern") or ""),
        node=str(raw.get("node") or "").strip().lower(),
        quota_patterns=_as_list(raw.get("quota_patterns")) if isinstance(
            raw.get("quota_patterns"), list) else ([str(raw["quota_patterns"])]
                                                   if raw.get("quota_patterns") else []),
    )


def normalize_software(config_value) -> dict[str, SoftwareSpec]:
    """把插件配置里的 software 规整成 {小写名字: SoftwareSpec}。"""
    items: list = []
    if isinstance(config_value, dict):
        items = list(config_value.items())
    elif isinstance(config_value, list):
        # ["codex exec {prompt}", ...]：用命令本身的名字当软件名
        for x in config_value:
            parts = str(x).split()
            if parts:
                base = os.path.splitext(os.path.basename(parts[0]))[0]
                items.append((base, str(x)))
    specs: dict[str, SoftwareSpec] = {}
    for name, raw in items:
        spec = _parse_one(name, raw)
        if spec:
            spec.name = spec.name.lower()
            specs[spec.name] = spec
    return specs


def normalize_projects(config_value) -> dict[str, str]:
    """projects 配置：{项目名: 目录}。"""
    if not isinstance(config_value, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip()
            for k, v in config_value.items() if str(k).strip() and str(v).strip()}
