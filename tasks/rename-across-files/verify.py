#!/usr/bin/env python3
"""rename-across-files 验收：Vec2 -> Vector2D，calc_force -> compute_force。

三段检查：1) 三个源文件能 compile；2) AST 级标识符扫描确认旧名彻底消失、新名就位；
3) 真导入并断言行为（期望值动态算自 sim.GRAVITY / sim.BODIES 这些数据本身）。
"""
from __future__ import annotations

import ast
import importlib
import math
import os
import re
import shutil
import sys
from pathlib import Path

WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
sys.dont_write_bytecode = True  # 别把 .pyc 写进任务目录或工作区
OLD_NAMES = ("Vec2", "calc_force")
NEW_CLASS = "Vector2D"
NEW_FUNC = "compute_force"
MODULES = ("vector", "physics", "sim")


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def sources() -> dict:
    """任务规定的三个源文件（agent 在工作区另建的杂项 .py 不参与旧名扫描）。"""
    found = {}
    for name in MODULES:
        path = WS / f"{name}.py"
        if not path.is_file():
            fail(f"缺少源文件 {name}.py")
        found[name] = path
    return found


def identifiers(tree: ast.AST) -> set:
    """AST 里的全部标识符（不含注释与 docstring 文字）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.name)
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


files = sources()

# 1) 语法检查 + 2) 旧标识符残留检查
for stem, path in files.items():
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        fail(f"{path.name} 语法错误: {exc}")
    used = identifiers(tree)
    for old in OLD_NAMES:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])")
        left = sorted(n for n in used if pattern.search(n))
        if left:
            fail(f"{path.name} 仍引用旧标识符 {old}（{', '.join(left)}）")

# 3) 真导入并断言行为
for cache in sorted(WS.rglob("__pycache__"), reverse=True):
    shutil.rmtree(cache, ignore_errors=True)  # 排除陈旧字节码缓存的干扰
sys.path.insert(0, str(WS))
for name in MODULES:
    sys.modules.pop(name, None)
try:
    vector = importlib.import_module("vector")
    physics = importlib.import_module("physics")
    sim = importlib.import_module("sim")
except Exception as exc:
    fail(f"导入模块失败: {type(exc).__name__}: {exc}")

for module, attr in ((vector, NEW_CLASS), (physics, NEW_FUNC), (physics, "momentum"),
                     (sim, "reported"), (sim, "total_weight")):
    if not hasattr(module, attr):
        fail(f"{module.__name__} 里找不到 {attr}（重命名未覆盖到该处）")
for old in OLD_NAMES:
    for module in (vector, physics, sim):
        if hasattr(module, old):
            fail(f"{module.__name__} 仍导出旧名 {old}，不允许保留兼容别名")

Vector2D = getattr(vector, NEW_CLASS)
compute_force = getattr(physics, NEW_FUNC)


def xy(vector) -> tuple:
    return (vector.x, vector.y)


def check(label, got, expected):
    if got != expected:
        fail(f"{label} 得到 {got!r}，期望 {expected!r}")


# 期望值只依赖模块里的数据，用纯 float 运算独立算出，不复用被测实现
try:
    gravity_x, gravity_y = float(sim.GRAVITY.x), float(sim.GRAVITY.y)
    bodies = [(float(mass), velocity) for mass, velocity in sim.BODIES]
except Exception as exc:
    fail(f"无法从 sim.GRAVITY / sim.BODIES 取到数据（是否被顺手改掉了？）: {exc}")
weight_x = sum(mass * gravity_x for mass, _velocity in bodies)
weight_y = sum(mass * gravity_y for mass, _velocity in bodies)
total_mass = sum(mass for mass, _velocity in bodies)
expected_line = (f"x={weight_x:.1f} y={weight_y:.1f} "
                 f"p={math.hypot(total_mass * weight_x, total_mass * weight_y):.1f}")

try:
    check("Vector2D(3, 4).length()", float(Vector2D(3, 4).length()), 5.0)
    check("Vector2D(3, 4).plus(Vector2D(1.5, -4))",
          xy(Vector2D(3, 4).plus(Vector2D(1.5, -4))), (4.5, 0))
    check("Vector2D(2.0, -3.0).scale(2.0)",
          xy(Vector2D(2.0, -3.0).scale(2.0)), (4.0, -6.0))
    force = compute_force(2.0, Vector2D(1.0, -5.0))
    check("compute_force(2.0, Vector2D(1.0, -5.0))", xy(force), (2.0, -10.0))
    if not isinstance(force, Vector2D):
        fail("compute_force 没有返回 Vector2D 实例")
    check("momentum(3.0, Vector2D(1.0, 2.0))",
          xy(physics.momentum(3.0, Vector2D(1.0, 2.0))), (3.0, 6.0))
    check("sim.total_weight()", xy(sim.total_weight()), (weight_x, weight_y))
    got_line = sim.reported()
except Exception as exc:
    fail(f"调用重命名后的 API 出错: {type(exc).__name__}: {exc}")
if got_line != expected_line:
    fail(f"sim.reported() 返回 {got_line!r}，期望 {expected_line!r}")

raise SystemExit(0)
