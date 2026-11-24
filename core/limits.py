"""额度识别：判断是不是撞上了用量上限，并尽量读出何时恢复。

只看输出的最后几十行，并把关键字分成两档，避免误判：
- 强特征（如 "usage limit reached"、"insufficient_quota"）：出现在末尾、且进程以非 0 退出
  （或整段输出只有寥寥几行）才算撞额度；
- 弱特征（如 "rate limit"、"429"、"额度"）：只有进程以非 0 退出、且出现在最后几行才算。
这样命令行 AI 在帮你写“限流中间件”、总结里提到 429 时，不会被当成额度用完。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

STRONG_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"usage limit (?:reached|exceeded|hit)",
    r"you'?ve hit your (?:usage |rate )?limit",
    r"you have hit your (?:usage |rate )?limit",
    r"(?:5-hour|five-hour|weekly|daily|session|opus|sonnet) limit (?:reached|exceeded)",
    r"limit will reset",
    r"insufficient_quota",
    r"exceeded your current quota",
    r"credit balance is too low",
    r"out of credits",
    r"额度(?:已)?(?:用完|用尽|耗尽|不足)",
    r"(?:已达|达到)(?:使用|调用|今日)?(?:额度)?上限",
    r"次数已达上限",
)]

WEAK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"rate[ _-]?limit", r"quota", r"\b429\b", r"too many requests",
    r"try again later", r"额度", r"请稍后再试", r"限流",
)]

STRONG_WINDOW_LINES = 25   # 强特征在最后多少行里找
WEAK_WINDOW_LINES = 8      # 弱特征在最后多少行里找
MAX_WAIT_SECONDS = 8 * 86400  # 周额度也能覆盖

_UNIT_SECONDS = {
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "天": 86400, "小时": 3600, "钟头": 3600, "个小时": 3600, "分钟": 60, "分": 60, "秒": 1,
}
_EN_UNIT = r"(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
_EN_PAIR = rf"\d+(?:\.\d+)?\s*{_EN_UNIT}"
_TRIGGER = r"(?:try again|retry|resets?|reset|available(?: again)?|wait|恢复)"

_REL_EN = re.compile(
    rf"{_TRIGGER}\s*(?:in|after|for)?\s*((?:{_EN_PAIR}[\s,]*(?:and\s+)?)+)", re.IGNORECASE)
_PAIR_EN = re.compile(rf"(\d+(?:\.\d+)?)\s*({_EN_UNIT[:-2]})\b", re.IGNORECASE)
_REL_ZH = re.compile(r"((?:\d+(?:\.\d+)?\s*(?:天|个小时|小时|钟头|分钟|分|秒)\s*)+)(?:之?后|以后)")
_PAIR_ZH = re.compile(r"(\d+(?:\.\d+)?)\s*(天|个小时|小时|钟头|分钟|分|秒)")
_EPOCH = re.compile(r"limit reached\|(\d{10})")

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_AT_RE = re.compile(
    rf"{_TRIGGER}\D{{0,20}}?"
    r"(?:(?P<ymd>\d{4}-\d{1,2}-\d{1,2})[ T,]*(?:at\s*)?"
    r"|(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s*)?)?"
    r"\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm)?\b",
    re.IGNORECASE,
)


def _tail_lines(text: str, n: int) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def looks_like_limit(text: str, exit_code: int = 1, extra_patterns=None) -> bool:
    """输出末尾是不是额度用完的样子。"""
    if not text:
        return False
    strong_win = _tail_lines(text, STRONG_WINDOW_LINES)
    # 强特征也要求“没有正常结束”：退出码非 0，或者整段输出只有寥寥几行（只打印了一句额度提示就退出的工具）。
    # 退出码 0 且输出正常的一轮，哪怕总结里写了 insufficient_quota，也不算撞额度。
    short = len([ln for ln in text.splitlines() if ln.strip()]) <= 5
    if exit_code == 0 and not short:
        strong_win = ""
    if strong_win and any(p.search(strong_win) for p in STRONG_PATTERNS):
        return True
    for pat in (extra_patterns or []) if strong_win else []:
        try:
            if re.search(pat, strong_win, re.IGNORECASE):
                return True
        except re.error:
            continue
    if exit_code != 0:
        weak_win = _tail_lines(text, WEAK_WINDOW_LINES)
        if any(p.search(weak_win) for p in WEAK_PATTERNS):
            return True
    return False


def _sum_pairs(chunk: str, pair_re, unit_lower=True) -> float:
    total = 0.0
    for num, unit in pair_re.findall(chunk):
        key = unit.lower() if unit_lower else unit
        total += float(num) * _UNIT_SECONDS.get(key, 60)
    return total


def parse_reset_time(text: str, now: float | None = None) -> float | None:
    """尽力解析什么时候可以继续，解析不出返回 None。"""
    if not text:
        return None
    window = _tail_lines(text, STRONG_WINDOW_LINES)
    now_ts = now or time.time()
    now_dt = datetime.fromtimestamp(now_ts)

    def ok(ts: float) -> bool:
        return 0 < ts - now_ts <= MAX_WAIT_SECONDS

    # ① Claude Code 老格式：Claude AI usage limit reached|1732370400
    for m in _EPOCH.finditer(window):
        ts = float(m.group(1))
        if ok(ts):
            return ts

    # ② 相对时间：try again in 2 hours 13 minutes / 45分钟后
    for m in _REL_EN.finditer(window):
        secs = _sum_pairs(m.group(1), _PAIR_EN)
        if ok(now_ts + secs):
            return now_ts + secs
    for m in _REL_ZH.finditer(window):
        secs = _sum_pairs(m.group(1), _PAIR_ZH, unit_lower=False)
        if ok(now_ts + secs):
            return now_ts + secs

    # ③ 绝对时间：resets at 3pm / try again at 15:05 / resets Nov 25, 9:00 AM
    for m in _AT_RE.finditer(window):
        hour, minute, ap = int(m.group("h")), int(m.group("mi") or 0), m.group("ap")
        if not ap and m.group("mi") is None:
            continue  # 光一个数字太容易误认，至少要 3pm 或 15:05 这种
        if ap:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ap.lower() == "pm" else 0)
        if hour > 23 or minute > 59:
            continue
        try:
            if m.group("ymd"):
                base = datetime.strptime(m.group("ymd"), "%Y-%m-%d")
            elif m.group("mon"):
                base = now_dt.replace(month=_MONTHS[m.group("mon").lower()[:3]],
                                      day=int(m.group("day")))
                if base.date() < now_dt.date():
                    base = base.replace(year=base.year + 1)
            else:
                base = now_dt
            dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            continue
        if not (m.group("ymd") or m.group("mon")) and dt <= now_dt:
            dt += timedelta(days=1)
        if ok(dt.timestamp()):
            return dt.timestamp()
    return None


def detect(text: str, fallback_minutes: int = 60, now: float | None = None,
           exit_code: int = 1, extra_patterns=None):
    """返回 (是否撞额度, 预计恢复时间戳, 恢复时间是不是猜的)。"""
    if not looks_like_limit(text, exit_code=exit_code, extra_patterns=extra_patterns):
        return False, None, False
    resume_at = parse_reset_time(text, now=now)
    if resume_at is not None:
        return True, resume_at, False
    return True, (now or time.time()) + max(1, fallback_minutes) * 60, True
