"""杂项工具：去 ANSI、切分长文本、人类可读时间。"""
from __future__ import annotations

import re
import time

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-_]"
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码和控制字符。"""
    if not text:
        return ""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_line(raw: bytes) -> str:
    """尽量猜对编码：先 UTF-8，再 GBK。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def chunk_text(text: str, limit: int = 1200) -> list[str]:
    """按行把长文本切成若干段。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = limit if limit and limit > 0 else 1200
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def human_duration(seconds: float) -> str:
    """把秒数写成 1小时2分3秒。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}分{s}秒" if s else f"{m}分"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分"


def human_time(ts: float) -> str:
    """时间戳转本地时间字符串。"""
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def preview(text: str, limit: int = 120) -> str:
    """截一句短预览。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_first(text: str) -> tuple[str, str]:
    """切出第一个词，剩下的部分原样保留（不会把换行、多个空格压成一个）。"""
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def safe_int(value, default: int, minimum: int | None = None) -> int:
    """把配置值转成 int，转不了就用默认值。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default
    if minimum is not None:
        num = max(minimum, num)
    return num
