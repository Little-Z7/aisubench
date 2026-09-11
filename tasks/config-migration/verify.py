#!/usr/bin/env python3
"""config-migration 验收：json.load 解析后与按规则算出的期望结构深层比较。

期望结构动态算自 fixtures/config.json，不写死字面量；额外断言关键值类型，
避免 true/1、"8080"/8080 这类蒙混过关。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("AISUBENCH_WORKSPACE") or Path.cwd())
V2_KEYS = {"version", "app", "server", "database", "logging", "network"}


def fail(msg: str):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


source = TASK_DIR / "fixtures" / "config.json"
if not source.is_file():
    source = WS / "config.json"
try:
    v1 = json.loads(source.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    fail(f"读取版本 1 参考配置 {source} 失败: {exc}")
if not isinstance(v1, dict):
    fail("参考配置顶层不是 JSON 对象")


def migrate(data: dict) -> dict:
    """任务说明里的 v1 -> v2 映射规则。"""
    return {
        "version": 2,
        "app": {"name": data["app_name"], "debug": data["debug"]},
        "server": {"host": data["host"], "port": data["port"]},
        "database": {"url": data["db_url"]},
        "logging": {"level": str(data["log_level"]).upper()},
        "network": {
            "allowed_hosts": [host.strip() for host in str(data["allowed_hosts"]).split(",")],
            "max_retries": data["max_retries"],
            "timeout_seconds": data["timeout_seconds"],
        },
    }


try:
    expected = migrate(v1)
except KeyError as exc:
    fail(f"参考配置里缺少字段 {exc}：需要以任务目录的 fixtures/config.json（版本 1）为基准，"
         f"但实际读到的 {source} 不是版本 1 格式")

target = WS / "config.json"
if not target.is_file():
    fail("缺少 config.json")
try:
    migrated = json.loads(target.read_text(encoding="utf-8"))
except ValueError as exc:
    fail(f"config.json 不是合法 JSON: {exc}")
if not isinstance(migrated, dict):
    fail(f"config.json 顶层必须是对象，实际是 {type(migrated).__name__}")

leftover = [key for key in v1 if key not in V2_KEYS and key in migrated]
if leftover:
    fail(f"版本 1 的顶层键仍然存在: {', '.join(sorted(leftover))}")
missing = V2_KEYS - set(migrated)
if missing:
    fail(f"顶层缺少版本 2 的键: {', '.join(sorted(missing))}")

if not is_int(migrated["version"]) or migrated["version"] != 2:
    fail(f"version 必须是整数 2，实际 {migrated['version']!r}")

app = migrated["app"]
if not isinstance(app, dict) or not isinstance(app.get("debug"), bool):
    fail(f"app.debug 必须是布尔值，实际 {app.get('debug')!r}")
server = migrated["server"]
if not is_int(server.get("port")):
    fail(f"server.port 必须是整数，实际 {server.get('port')!r}")
hosts = migrated["network"].get("allowed_hosts")
if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
    fail(f"network.allowed_hosts 必须是字符串列表，实际 {hosts!r}")
for number_key in ("max_retries", "timeout_seconds"):
    value = migrated["network"].get(number_key)
    if not is_int(value):
        fail(f"network.{number_key} 必须是整数，实际 {value!r}")

if migrated != expected:
    fail(f"迁移结果与规范不一致：\n实际   = {json.dumps(migrated, sort_keys=True, ensure_ascii=False)}"
         f"\n期望 = {json.dumps(expected, sort_keys=True, ensure_ascii=False)}")

raise SystemExit(0)
