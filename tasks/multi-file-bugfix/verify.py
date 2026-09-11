#!/usr/bin/env python3
"""multi-file-bugfix 验收：小计 -> 按比例折扣 -> 加税 这条链路必须真正跑通。

期望金额用 fixtures 里的 ORDERS / TAX_RATES 独立算出，不复用被测代码。
"""
from __future__ import annotations

import importlib
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
sys.dont_write_bytecode = True  # 别把 .pyc 写进任务目录或工作区
MODULES = ("cart", "tax", "orders", "checkout")


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def close(got, expected) -> bool:
    return isinstance(got, (int, float)) and math.isclose(float(got), float(expected),
                                                           rel_tol=1e-9, abs_tol=1e-9)


def fixture_module(stem: str):
    """按路径导入 fixtures 里的原始版本，作为参考数据。"""
    import importlib.util

    path = TASK_DIR / "fixtures" / f"{stem}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"aisubench_ref_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


for name in MODULES:
    if not (WS / f"{name}.py").is_file():
        fail(f"缺少源文件 {name}.py")

# 用工作区版本导入被测模块（cart/tax/checkout 之间有裸名互相 import）
for cache in sorted(WS.rglob("__pycache__"), reverse=True):
    shutil.rmtree(cache, ignore_errors=True)  # 排除陈旧字节码缓存的干扰
sys.path.insert(0, str(WS))
for name in MODULES:
    sys.modules.pop(name, None)
try:
    cart = importlib.import_module("cart")
    tax = importlib.import_module("tax")
    orders = importlib.import_module("orders")
    checkout = importlib.import_module("checkout")
except Exception as exc:
    fail(f"导入被测模块失败: {type(exc).__name__}: {exc}")

ref_orders = fixture_module("orders")
ref_tax = fixture_module("tax")
if ref_orders is None or ref_tax is None:  # 旧版 runner 场景：退化为工作区数据
    ref_orders, ref_tax = orders, tax

if getattr(cart, "sub_total", None) is not None:
    fail("cart.py 里出现了 sub_total：正确的函数名是 subtotal")
for module, attr in ((cart, "subtotal"), (cart, "apply_discount"),
                     (tax, "add_tax"), (checkout, "order_total")):
    if not callable(getattr(module, attr, None)):
        fail(f"{module.__name__}.py 里没有可调用的 {attr}")

if tax.TAX_RATES != ref_tax.TAX_RATES:
    fail(f"TAX_RATES 的数值被改动：{tax.TAX_RATES!r} != {ref_tax.TAX_RATES!r}")
if orders.ORDERS != ref_orders.ORDERS:
    fail("orders.py 里的 ORDERS 数据被改动")

ITEMS = [{"price": 10.0, "qty": 2}, {"price": 2.5, "qty": 4}]


def ref_total(items, percent, band):
    amount = sum(item["price"] * item["qty"] for item in items)
    amount = amount * (1 - percent / 100)
    return amount * (1 + ref_tax.TAX_RATES[band])


CASES = [
    (ITEMS, 0, "exempt"),
    (ITEMS, 50, "reduced"),
    (ITEMS, 15, "standard"),
    ([], 15, "standard"),
    (orders.ORDERS, 15, "standard"),
    (orders.ORDERS, 100, "exempt"),
]
for items, percent, band in CASES:
    expected = ref_total(items, percent, band)
    try:
        got = checkout.order_total(items, percent, band)
    except Exception as exc:
        fail(f"order_total(折扣 {percent}%, 档 {band}) 抛出 {type(exc).__name__}: {exc}")
    if not close(got, expected):
        fail(f"order_total(折扣 {percent}%, 档 {band}) 返回 {got!r}，期望 {expected!r}")

def call(fn, args, label):
    try:
        return fn(*args)
    except Exception as exc:
        fail(f"{label} 抛出 {type(exc).__name__}: {exc}")


POINT_CASES = [
    (cart.subtotal, (ITEMS,), 30.0, "subtotal(ITEMS)"),
    (cart.subtotal, ([],), 0, "subtotal([])"),
    (cart.apply_discount, (200, 15), 170.0, "apply_discount(200, 15)"),
    (cart.apply_discount, (80, 0), 80.0, "apply_discount(80, 0)"),
    (cart.apply_discount, (80, 100), 0.0, "apply_discount(80, 100)"),
    (tax.add_tax, (100, "reduced"), 105.0, "add_tax(100, reduced)"),
    (tax.add_tax, (50, "exempt"), 50.0, "add_tax(50, exempt)"),
]
for fn, args, expected, label in POINT_CASES:
    got = call(fn, args, label)
    if not close(got, expected):
        fail(f"{label} 返回 {got!r}，期望 {expected!r}")

try:
    tax.add_tax(10, "nope")
except ValueError:
    pass
except Exception as exc:
    fail(f"未知税率档应抛 ValueError，实际抛 {type(exc).__name__}")
else:
    fail("未知税率档没有抛 ValueError")

try:
    completed = subprocess.run([sys.executable, "checkout.py"], cwd=WS, shell=False,
                               text=True, capture_output=True, timeout=15)
except Exception as exc:
    fail(f"执行 python3 checkout.py 失败: {type(exc).__name__}: {exc}")
if completed.returncode != 0:
    detail = (completed.stderr or completed.stdout).strip()
    fail(f"checkout.py 退出码 {completed.returncode}：{detail}")
lines = [line for line in completed.stdout.splitlines() if line.strip()]
expected_line = f"TOTAL={ref_total(ref_orders.ORDERS, 15, 'standard'):.2f}"
if len(lines) != 1:
    fail(f"checkout.py 应只输出一行，实际 {completed.stdout!r}")
if lines[0].strip() != expected_line:
    fail(f"checkout.py 输出 {lines[0]!r}，期望 {expected_line!r}")

raise SystemExit(0)
