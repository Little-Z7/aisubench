#!/usr/bin/env python3
"""echo-file 验收：output.txt 必须与 fixtures/input.txt 字节完全一致。

期望内容动态读自 fixtures/input.txt，不写死文本。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def original(name: str) -> bytes:
    path = TASK_DIR / "fixtures" / name
    if not path.is_file():
        path = WS / name
    try:
        return path.read_bytes()
    except OSError as exc:
        fail(f"读取 {path} 失败: {exc}")


expected = original("input.txt")

current = WS / "input.txt"
if current.exists() and current.read_bytes() != expected:
    fail("输入文件 input.txt 被改动，验收要求原样保留")

out = WS / "output.txt"
if not out.is_file():
    fail("缺少输出文件 output.txt")
given = out.read_bytes()
if given != expected:
    fail(f"output.txt 与 input.txt 不一致：期望 {expected!r}，实际 {given!r}")

raise SystemExit(0)
