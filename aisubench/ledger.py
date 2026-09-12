"""持续监测账本：采样侧（``watch``）与估计侧（``estimate``）共享的 JSONL 样本存储。

每行一个样本，契约与估计器已定死（另见 ``estimate.py`` 模块 docstring）::

    {"ts": 1726000000.0, "agent": "kimi", "subscription": "demo", "source": "watch",
     "usage": {"input": 100, "cached": 900, "output": 50, "requests": 2},
     "pools": {"5h": 12.0, "week": 34.0},
     "clean": true}

- ``usage`` 为自同 agent 上一个样本以来的 token 增量：``input`` 是非缓存输入、
  ``cached`` 是缓存命中、``requests`` 是请求条数；
- ``pools`` 为采样时刻各额度池的已用百分比快照；
- ``clean`` 表示该采样区间内消耗全部被 meter 观测到（口径见 ``watch`` docstring）；
- ``subscription`` 为样本归属的订阅名（``[agents.X].subscription``，缺省为 agent 名；
  旧账本样本无该字段，读取侧归 ``default`` 分组）。

默认路径 ``state/ledger.jsonl``；``state/`` 已被 git 忽略。本模块只做存储，
不 import ``estimate``（估计器只读账本，两侧通过上述契约解耦）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ROOT

DEFAULT_LEDGER = ROOT / "state" / "ledger.jsonl"


def append_sample(path: str | Path, sample: dict) -> None:
    """追加一条样本（一行 JSON），父目录自动创建。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, ensure_ascii=False) + "\n")


def load_samples(path: str | Path) -> list[dict]:
    """读取全部样本：坏行/非对象行跳过，按 ``ts`` 升序排序（缺 ``ts`` 排最后）。"""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    samples: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            samples.append(parsed)
    return sorted(samples, key=_sort_key)


def latest_sample(path: str | Path) -> dict | None:
    """返回 ``ts`` 最新的样本；账本不存在或无有效样本时为 None。"""
    samples = load_samples(path)
    return samples[-1] if samples else None


def _sort_key(sample: dict[str, Any]) -> tuple[bool, float]:
    value = sample.get("ts")
    if isinstance(value, bool):
        return (True, 0.0)
    try:
        return (False, float(value))
    except (TypeError, ValueError):
        return (True, 0.0)
