#!/usr/bin/env python3
"""implement-from-docstring 验收：square(value) 必须返回 value 的平方。

行为化验证：真导入并断言多组输入输出，不看源码文本。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
sys.dont_write_bytecode = True  # 别把 .pyc 写进任务目录或工作区
MODULE_NAME = "aisubench_docstring_under_test"

CASES = [
    ((0,), 0),
    ((1,), 1),
    ((7,), 49),
    ((-9,), 81),
    ((1.5,), 2.25),
    ((-0.5,), 0.25),
    ((1000000,), 1000000000000),
]


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


path = WS / "solution.py"
if not path.is_file():
    fail("缺少 solution.py")

try:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception as exc:
    fail(f"导入 solution.py 失败: {type(exc).__name__}: {exc}")

square = getattr(module, "square", None)
if not callable(square):
    fail("solution.py 里没有可调用的 square 函数")

for args, expected in CASES:
    try:
        got = square(*args)
    except Exception as exc:
        fail(f"square{args} 抛出 {type(exc).__name__}: {exc}")
    if got != expected:
        fail(f"square{args} 返回 {got!r}，期望 {expected!r}")

raise SystemExit(0)
