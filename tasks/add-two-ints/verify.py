#!/usr/bin/env python3
"""add-two-ints 验收：answer.txt 必须是 values.txt 中两个整数之和。

期望值动态算自 fixtures/values.txt，不在脚本里写死答案。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def original(name: str) -> Path:
    path = TASK_DIR / "fixtures" / name
    return path if path.exists() else WS / name


values_path = original("values.txt")
try:
    raw = values_path.read_text(encoding="utf-8")
except OSError as exc:
    fail(f"读取 {values_path} 失败: {exc}")

parts = raw.split()
if len(parts) != 2:
    fail(f"values.txt 应恰好包含两个整数，实际 {len(parts)} 个字段")
try:
    expected = int(parts[0]) + int(parts[1])
except ValueError as exc:
    fail(f"values.txt 不是两个整数: {exc}")

current = WS / "values.txt"
if current.exists() and current.read_text(encoding="utf-8") != raw:
    fail("输入文件 values.txt 被改动，验收要求原样保留")

answer_path = WS / "answer.txt"
if not answer_path.is_file():
    fail("缺少输出文件 answer.txt")
text = answer_path.read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[+-]?\d+", text):
    fail(f"answer.txt 必须只含一个整数，实际内容: {text!r}")
given = int(text)
if given != expected:
    fail(f"答案不对: answer.txt={given}，期望 {expected}")

raise SystemExit(0)
