from __future__ import annotations

from datetime import datetime
from typing import Any


def total_tokens(usage: dict[str, Any]) -> int:
    """Usage 口径：input 为非缓存输入，cache_creation 计入 input，
    cached 为缓存命中；total = input + cached + output。
    """
    if "input" in usage or "output" in usage:
        return (_count(usage.get("input", 0)) + _count(usage.get("cached", 0))
                + _count(usage.get("output", 0)))
    return _count(usage.get("total", 0))


def tps_gen(usage: dict[str, Any]) -> float:
    first, last = _seconds(usage.get("first_token_ts")), _seconds(usage.get("last_token_ts"))
    if first is None or last is None:
        return 0.0
    if (usage.get("first_token_ts") is not None
            and usage.get("last_token_ts") is not None
            and _is_iso(usage.get("first_token_ts")) != _is_iso(usage.get("last_token_ts"))):
        return 0.0
    seconds = last - first
    return _count(usage.get("output", 0)) / seconds if seconds > 0 else 0.0


def tps_wall(usage: dict[str, Any], wall_time: float | None = None) -> float:
    raw_seconds = wall_time if wall_time is not None else usage.get("wall_time", 0)
    try:
        seconds = float(raw_seconds)
    except (TypeError, ValueError):
        seconds = 0
    return total_tokens(usage) / seconds if seconds > 0 else 0.0


def task_metrics(results: list[dict], price: float | None = None,
                 quota_tokens: float | None = None) -> dict:
    count = len(results)
    passed = [item for item in results
              if item.get("converged", item.get("verify", {}).get("passed", False))]
    all_tokens = sum(total_tokens(item.get("usage", {})) for item in results)
    passed_tokens = sum(total_tokens(item.get("usage", {})) for item in passed)
    data = {
        "tasks": count,
        "passed": len(passed),
        "pass_rate": len(passed) / count if count else None,
        "total_tokens": all_tokens,
        "passed_tokens": passed_tokens,
        "tokens_per_task": all_tokens / count if count else None,
        "tokens_per_passed_task": passed_tokens / len(passed) if passed else None,
    }
    if price is not None and quota_tokens is not None and quota_tokens > 0:
        unit = price / quota_tokens
        tokens_per_task = data["tokens_per_task"]
        data["yuan_per_task"] = tokens_per_task * unit if tokens_per_task is not None else None
        data["yuan_per_effective_task"] = all_tokens / len(passed) * unit if passed else None
    return data


def _is_iso(value: Any) -> bool:
    return isinstance(value, str)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
