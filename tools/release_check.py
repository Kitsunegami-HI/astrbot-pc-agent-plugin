"""发版前检查：扫描发布包（zip）或插件目录，确认没有运行时产物和私人信息。

用法（在插件目录的上一级执行）：
    python astrbot_plugin_pc_agent/tools/release_check.py astrbot_plugin_pc_agent.zip      # 扫已打好的包
    python astrbot_plugin_pc_agent/tools/release_check.py --build out.zip astrbot_plugin_pc_agent
        # 按排除规则打包，再扫一遍刚打好的包
退出码：0 = 通过（可能有需要人工看一眼的“提示”）；1 = 发现必须处理的问题。
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
import zipfile

# 文件名规则：命中任何一条都不许进包
FORBIDDEN = [
    "*/node_data/*", "node_data/*", "*/spool.db", "*/spool.db-*", "*/client_config.json",
    "*.log", "*/__pycache__/*", "*.pyc", "*.pyo", "*/.DS_Store", "*/Thumbs.db", "*/node.lock",
    "*/tasks.json", "*/nodes_state.json", "*/.env", "*.pem", "*.key", "*.tmp", "*/.git/*",
    "*/.windows-build/*",
]
# 内容规则：必须处理的
HARD = [
    (re.compile(r"/(?:work|home|root|Users)/[A-Za-z0-9_.-]+/"), "本机绝对路径"),
    (re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9_.-]+"), "Windows 用户目录路径"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱地址"),
    (re.compile(r'"token"\s*:\s*"(?![^"]*(?:随机|token|填|example|xxx|t0ken))[^"]{6,}"', re.I), "看起来是真实 token"),
    (re.compile(r"(?:sk|ghp|gho|github_pat|xox[bp])[-_][A-Za-z0-9_]{16,}"), "疑似 API 密钥"),
]
# 内容规则：只提示，人工确认
SOFT = [
    (re.compile(r"(?<![\dxX.])[1-9]\d{6,10}(?![\d.])"), "长数字（是不是 QQ 号？）"),
    (re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|top|xyz|cc)\b", re.I), "域名"),
]
SAFE_DOMAINS = {"example.com", "bot.example.com", "www.msftconnecttest.com", "msftconnecttest.com",
                "claude.com", "github.com", "www.baidu.com"}
SAFE_NUMBERS = {"1732370400", "1800000000", "1000000"}  # 文档里的额度时间戳示例等
EXCLUDE_DIRS = {"__pycache__", "node_data", ".git", ".windows-build"}


def forbidden(name: str) -> bool:
    n = "/" + name.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatch(n, "*/" + pat.lstrip("*/")) or fnmatch.fnmatch(n, pat) for pat in FORBIDDEN)


def iter_files(path: str):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield info.filename, zf.read(info)
    else:
        base = os.path.dirname(os.path.abspath(path))
        for root, _, files in os.walk(path):
            for f in files:
                full = os.path.join(root, f)
                with open(full, "rb") as fp:
                    yield os.path.relpath(full, base), fp.read()


def scan(path: str) -> int:
    hard, soft = [], []
    me = os.path.basename(__file__)
    for name, data in iter_files(path):
        if forbidden(name):
            hard.append(f"{name}: 不该进包的文件")
            continue
        if name.endswith(me):
            continue  # 规则本身
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for rx, why in HARD:
                if rx.search(line):
                    hard.append(f"{name}:{i}: {why}: {line.strip()[:100]}")
            for rx, why in SOFT:
                for m in rx.finditer(line):
                    v = m.group(0).lower()
                    if v in SAFE_DOMAINS or v in SAFE_NUMBERS or v.endswith((".py", ".json", ".md", ".js")):
                        continue
                    soft.append(f"{name}:{i}: {why}: {m.group(0)}")
    for h in hard:
        print("✗ " + h)
    for s in soft:
        print("? " + s)
    print(f"结果：{len(hard)} 个必须处理的问题，{len(soft)} 条需要人工确认的提示")
    return 1 if hard else 0


def build(out: str, src: str) -> None:
    src = os.path.abspath(src)
    base = os.path.dirname(src)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
            for f in sorted(files):
                rel = os.path.relpath(os.path.join(root, f), base)
                if not forbidden(rel):
                    zf.write(os.path.join(root, f), rel)
    print(f"已打包：{out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--build"] and len(args) == 3:
        build(args[1], args[2])
        sys.exit(scan(args[1]))
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    sys.exit(scan(args[0]))
