#!/usr/bin/env python3
"""log-parse-script 验收：真实运行 parse_log.py，并比对它写出的 summary.txt。

期望条数动态统计自 fixtures/app.log；再把脚本连同追加过的日志复制到临时目录跑一遍，
确认它是真在统计而不是把条数写死。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
LEVELS = ("INFO", "WARN", "ERROR")
PROBE_EXTRA = ("2026-05-03 09:00:00 ERROR boom\n"
               "2026-05-03 09:00:01 WARN careful\n"
               "2026-05-03 09:00:02 INFO ok\n"
               "2026-05-03 09:00:03 ERROR bang\n")


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def original(name: str) -> Path:
    path = TASK_DIR / "fixtures" / name
    return path if path.exists() else WS / name


def counts_of(text: str) -> dict:
    counts = {level: 0 for level in LEVELS}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] in counts:
            counts[fields[2]] += 1
    return counts


source_log = original("app.log")
try:
    log_text = source_log.read_text(encoding="utf-8")
except OSError as exc:
    fail(f"读取 {source_log} 失败: {exc}")
expected = counts_of(log_text)
expected_text = "".join(f"{level}={expected[level]}\n" for level in LEVELS)
probe_expected = counts_of(log_text + PROBE_EXTRA)
probe_expected_text = "".join(f"{level}={probe_expected[level]}\n" for level in LEVELS)
if probe_expected_text == expected_text:
    fail("测试数据有问题：追加日志后期望值没有变化，无法验证脚本是否真在统计")

workspace_log = WS / "app.log"
if workspace_log.exists() and workspace_log.read_text(encoding="utf-8") != log_text:
    fail("输入文件 app.log 被改动，验收要求原样保留")

script = WS / "parse_log.py"
if not script.is_file():
    fail("缺少脚本 parse_log.py")
try:
    compile(script.read_text(encoding="utf-8"), str(script), "exec")
except SyntaxError as exc:
    fail(f"parse_log.py 语法错误: {exc}")

summary = WS / "summary.txt"
if summary.exists():
    summary.unlink()  # 强制由脚本重新生成，防止手写文件蒙混

try:
    completed = subprocess.run([sys.executable, "parse_log.py"], cwd=WS, shell=False,
                               text=True, capture_output=True, timeout=15)
except Exception as exc:
    fail(f"运行 parse_log.py 失败: {type(exc).__name__}: {exc}")
if completed.returncode != 0:
    detail = (completed.stderr or completed.stdout).strip()
    fail(f"parse_log.py 退出码 {completed.returncode}：{detail}")

if not summary.is_file():
    fail("parse_log.py 运行后没有生成 summary.txt")
given = summary.read_text(encoding="utf-8")
if given != expected_text:
    fail(f"summary.txt 内容不对：期望 {expected_text!r}，实际 {given!r}")

# 换一份日志再跑一次：脚本必须真在统计，而不是把条数写死
with tempfile.TemporaryDirectory(prefix="aisubench-log-") as tmp:
    tmp_dir = Path(tmp)
    (tmp_dir / "app.log").write_text(log_text + PROBE_EXTRA, encoding="utf-8")
    shutil.copy2(script, tmp_dir / script.name)
    try:
        again = subprocess.run([sys.executable, script.name], cwd=tmp_dir, shell=False,
                               text=True, capture_output=True, timeout=15)
    except Exception as exc:
        fail(f"在临时目录运行 {script.name} 失败: {type(exc).__name__}: {exc}")
    if again.returncode != 0:
        detail = (again.stderr or again.stdout).strip()
        fail(f"换日志后 {script.name} 退出码 {again.returncode}：{detail}")
    probe = tmp_dir / "summary.txt"
    if not probe.is_file():
        fail(f"换日志后 {script.name} 没有写出当前目录的 summary.txt")
    got_probe = probe.read_text(encoding="utf-8")
    if got_probe != probe_expected_text:
        fail(f"脚本不是真的在统计：换日志后 summary.txt 为 {got_probe!r}，期望 {probe_expected_text!r}")

raise SystemExit(0)
