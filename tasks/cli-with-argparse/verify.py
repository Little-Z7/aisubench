#!/usr/bin/env python3
"""cli-with-argparse 验收：真实执行 cli.py，断言退出码 / stdout / stderr。

期望值动态统计自 fixtures/numbers.txt；另外用一份临时数据文件再跑一遍，
防止把结果硬编码进 cli.py。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
ALT_DATA = ["100", "-50", "200"]  # sum=250 max=200 count=3


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def numbers_from(text: str) -> list:
    values = []
    for line in text.splitlines():
        for field in line.split():
            try:
                values.append(int(field))
            except ValueError:
                fail(f"数据文件里出现非整数字段 {field!r}，测试数据被改坏了")
    return values


source = TASK_DIR / "fixtures" / "numbers.txt"
if not source.is_file():
    source = WS / "numbers.txt"
try:
    fixture_text = source.read_text(encoding="utf-8")
except OSError as exc:
    fail(f"读取 {source} 失败: {exc}")
fixture_numbers = numbers_from(fixture_text)
if len(fixture_numbers) < 2:
    fail("数据文件至少要有两个整数，否则验收没有区分度")

current = WS / "numbers.txt"
if current.exists() and current.read_text(encoding="utf-8") != fixture_text:
    fail("输入文件 numbers.txt 被改动，验收要求原样保留")

cli = WS / "cli.py"
if not cli.is_file():
    fail("缺少 cli.py")
try:
    compile(cli.read_text(encoding="utf-8"), str(cli), "exec")
except SyntaxError as exc:
    fail(f"cli.py 语法错误: {exc}")


def run(args, cwd=WS) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([sys.executable, "cli.py", *args], cwd=cwd, shell=False,
                              text=True, capture_output=True, timeout=10)
    except Exception as exc:
        fail(f"运行 python3 cli.py {' '.join(args)} 失败: {type(exc).__name__}: {exc}")


def check_ok(args, expected: int, label: str):
    done = run(args)
    if done.returncode != 0:
        fail(f"{label}: 退出码 {done.returncode}（期望 0），stderr={done.stderr.strip()!r}")
    if done.stdout != f"{expected}\n":
        fail(f"{label}: stdout={done.stdout!r}，期望 {expected!r} 加一个换行")


CASES = [
    (["numbers.txt", "--op", "sum"], sum(fixture_numbers), "--op sum"),
    (["numbers.txt", "--op", "max"], max(fixture_numbers), "--op max"),
    (["numbers.txt", "--op", "count"], len(fixture_numbers), "--op count"),
]

with tempfile.TemporaryDirectory(prefix="aisubench-cli-") as tmp:
    alt = Path(tmp) / "alt.txt"
    alt.write_text("".join(f"{n}\n" for n in ALT_DATA), encoding="utf-8")
    alt_numbers = numbers_from(alt.read_text(encoding="utf-8"))
    CASES += [
        ([str(alt), "--op", "sum"], sum(alt_numbers), "换数据文件 --op sum"),
        ([str(alt), "--op", "max"], max(alt_numbers), "换数据文件 --op max"),
        ([str(alt), "--op", "count"], len(alt_numbers), "换数据文件 --op count"),
    ]
    empty = Path(tmp) / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")

    for args, expected, label in CASES:
        check_ok(args, expected, label)

    # 文件不存在 / 文件里没有整数 -> 退出码 1，stderr 以 error: 开头
    for args, label in ((["missing-file.txt", "--op", "sum"], "文件不存在"),
                        ([str(empty), "--op", "sum"], "空数据文件")):
        done = run(args)
        if done.returncode != 1:
            fail(f"{label}: 退出码 {done.returncode}（期望 1），stdout={done.stdout!r}")
        if done.stdout:
            fail(f"{label}: stdout 应为空，实际 {done.stdout!r}")
        if not done.stderr.startswith("error:"):
            fail(f"{label}: stderr 应以 error: 开头，实际 {done.stderr!r}")

    # 参数错误 -> argparse 的退出码 2，usage 出现在 stderr
    for args, label in ((["--op", "sum"], "缺少 FILE"),
                        (["numbers.txt"], "缺少 --op"),
                        (["numbers.txt", "--op", "median"], "--op 取值非法")):
        done = run(args)
        if done.returncode != 2:
            fail(f"{label}: 退出码 {done.returncode}（期望 2），stdout={done.stdout!r}")
        if "usage" not in done.stderr.lower():
            fail(f"{label}: stderr 里应看到 argparse 的 usage，实际 {done.stderr!r}")
        if done.stdout:
            fail(f"{label}: 参数错误时 stdout 应为空，实际 {done.stdout!r}")

    done = run([])
    if done.returncode != 2 or "usage" not in done.stderr.lower():
        fail(f"不带任何参数运行时应由 argparse 报 usage 并退出 2，实际 rc={done.returncode} "
             f"stderr={done.stderr!r}")

raise SystemExit(0)
