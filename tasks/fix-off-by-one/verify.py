#!/usr/bin/env python3
"""fix-off-by-one 验收：total(values) 必须返回全部元素之和。

行为化验证：真导入并断言多组输入，含空序列、负数、tuple、生成器。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
sys.dont_write_bytecode = True  # 别把 .pyc 写进任务目录或工作区
MODULE_NAME = "aisubench_off_by_one_under_test"


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


path = WS / "solution.py"
if not path.is_file():
    fail("缺少 solution.py")

source = path.read_text(encoding="utf-8")
try:
    compile(source, str(path), "exec")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception as exc:
    fail(f"导入 solution.py 失败: {type(exc).__name__}: {exc}")

total = getattr(module, "total", None)
if not callable(total):
    fail("solution.py 里没有可调用的 total 函数")

CASES = [
    ("[]", [], 0),
    ("[7]", [7], 7),
    ("[1, 2, 3, 4]", [1, 2, 3, 4], 10),
    ("[-3, -4]", [-3, -4], -7),
    ("[-5, 5, 10]", [-5, 5, 10], 10),
    ("(2, 4, 6) tuple", (2, 4, 6), 12),
    ("生成器", (n for n in (10, 20, 30)), 60),
    ("range(1, 101)", list(range(1, 101)), 5050),
    ("空 tuple", (), 0),
]

for label, args, expected in CASES:
    try:
        got = total(args)
    except Exception as exc:
        fail(f"total({label}) 抛出 {type(exc).__name__}: {exc}")
    if got != expected:
        fail(f"total({label}) 返回 {got!r}，期望 {expected!r}")

raise SystemExit(0)
