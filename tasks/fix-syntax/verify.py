#!/usr/bin/env python3
"""fix-syntax 验收：fixed.py 必须能编译，且 add() 的返回值正确。

不是子串匹配：先 compile() 抓语法错误，再真导入并逐组断言返回值。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
sys.dont_write_bytecode = True  # 别把 .pyc 写进任务目录或工作区
MODULE_NAME = "aisubench_fix_syntax_under_test"

CASES = [
    ((2, 3), 5),
    ((0, 0), 0),
    ((-4, 9), 5),
    ((-7, -8), -15),
    ((1234, 5678), 6912),
    ((-999999999, 1), -999999998),
]


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


path = WS / "fixed.py"
if not path.is_file():
    fail("缺少 fixed.py（请把修复后的文件保存为工作区的 fixed.py）")

source = path.read_text(encoding="utf-8")
try:
    compile(source, str(path), "exec")
except SyntaxError as exc:
    fail(f"fixed.py 仍有语法错误: {exc}")

try:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception as exc:
    fail(f"导入 fixed.py 失败: {type(exc).__name__}: {exc}")

add = getattr(module, "add", None)
if not callable(add):
    fail("fixed.py 里没有可调用的 add 函数")

for args, expected in CASES:
    try:
        got = add(*args)
    except Exception as exc:
        fail(f"add{args} 抛出 {type(exc).__name__}: {exc}")
    if got != expected:
        fail(f"add{args} 返回 {got!r}，期望 {expected!r}")

raise SystemExit(0)
