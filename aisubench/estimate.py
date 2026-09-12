"""额度池估计器：从持续采样的账本样本里估计 token 与额度百分比的换算关系。

样本契约（采样侧与估计侧共享，本模块只读）：
    {"ts": epoch 秒, "agent": str, "source": str,
     "usage": {"input": 非缓存输入, "cached": 缓存命中, "output": 输出,
               "requests": 次数},   # 自上一个样本以来的增量
     "pools": {"5h": 12.0, "week": 34.0},  # 采样时刻各窗口的已用百分比快照
     "clean": true}  # 该区间内全部消耗都被 meter 观测到；false 表示可能混入外部消耗

口径与假设：
- usage 的 token 总量沿用 metrics.total_tokens，即 total = input + cached + output。
- 相邻样本对 (s[i], s[i+1]) 的池增量 Δ = s[i+1].pools[p] − s[i].pools[p]，
  对应消耗取 s[i+1].usage（该区间的增量）。
- Δ < 0 说明池发生窗口滚动/周期重置，该对不参与比率估计，仅使 resets 计数 +1。
- 点估计为比率估计 R = Σtokens / ΣΔ，只在 Δ > 0 的有效对上累加（并非各对比率的平均）。
- 区间沿用 calibrate 的 ±g 读数误差口径：每次量化读数误差 ±g/2，Δ 最坏误差 ±g，
  故 R_low = Σtokens/(ΣΔ+g)、R_high = Σtokens/(ΣΔ−g)；ΣΔ ≤ g 时上界不可用（None）。
- Q 为整池 100% 的 token 当量，Q = 100×R；q_low/q_high 同理。
- 所有缺失字段（pools 缺池、usage 缺键、ts 缺失）都按不可用处理，不抛异常。
"""

from __future__ import annotations

import math
from typing import Any

from .metrics import total_tokens

_META_KEY = "_meta"

# burn_rate 的最小样本跨度：亚秒级间隔会放大出几十万 tok/h 的伪速率，
# 跨度不足该值视为数据不足（返回 None）。
MIN_RATE_SPAN_SEC = 60.0


def estimate_pools(samples: list[dict], granularity_pct: float = 1.0,
                   clean_only: bool = False) -> dict[str, dict]:
    """按池估计 tokens/% 比率及其置信区间。

    granularity_pct 为读数最小刻度 g（必须为正）。clean_only=True 时只保留
    两端样本均 clean 的相邻对，用于排除网页/手机端等未观测渠道的污染。

    返回每个池一个 dict：{tokens_per_pct, quota_tokens, q_low, q_high,
    n_pairs, resets, total_tokens, available}，另有 "_meta" 键给出
    {n_samples, span_hours, external_tokens}。其中：
    - tokens_per_pct 为 R（区间中点估计），quota_tokens = 100×R；
    - n_pairs 为参与估计的有效对数量，resets 为检测到的重置次数；
    - total_tokens 为有效对累计的 token 增量；
    - available=False 表示样本不足以给出比率（如 tokens>0 但 ΣΔ=0、
      或没有任何有效对），此时不输出伪 0 值，数值字段为 None；
    - q_high 在 ΣΔ ≤ g 时为 None（上界不可用）。
    """
    granularity = _number(granularity_pct)
    if granularity is None or granularity <= 0:
        raise ValueError("granularity_pct 必须为正数")

    ordered = _sorted_samples(samples)
    pool_names = sorted({name for sample in ordered for name in _pools(sample)})
    stats: dict[str, dict[str, float]] = {
        name: {"tokens": 0, "delta": 0.0, "n_pairs": 0, "resets": 0}
        for name in pool_names
    }

    for before, after in zip(ordered, ordered[1:]):
        if clean_only and not (_clean(before) and _clean(after)):
            continue
        tokens = _usage_total(after)
        before_pools, after_pools = _pools(before), _pools(after)
        for name in pool_names:
            if name not in before_pools or name not in after_pools:
                continue
            delta = after_pools[name] - before_pools[name]
            if delta < 0:
                stats[name]["resets"] += 1
                continue
            if delta == 0:
                continue
            stats[name]["tokens"] += tokens
            stats[name]["delta"] += delta
            stats[name]["n_pairs"] += 1

    result: dict[str, dict] = {
        name: _summarize(stat, granularity)
        for name, stat in stats.items()
    }
    result[_META_KEY] = _meta(ordered)
    return result


def burn_rate(samples: list[dict], pool: str, window_hours: float) -> float | None:
    """最近 window_hours 内该池对应样本的平均消耗速率（tokens/小时）。

    以全部含该池的样本中最新的 ts 为窗口右端；窗口内样本少于 2 个、
    或时间跨度小于 MIN_RATE_SPAN_SEC（60 秒，含窗口非正与跨度为 0）
    时返回 None——亚秒级跨度会把速率放大成天文数字伪值，视为数据不足。
    速率 = 窗口内样本的 token 增量之和 ÷ 实际时间跨度，因此不依赖
    固定采样间隔。
    """
    window = _number(window_hours)
    if not pool or window is None or window <= 0:
        return None
    rows: list[tuple[float, int]] = []
    for sample in samples or []:
        stamp = _ts(sample)
        if stamp is None or pool not in _pools(sample):
            continue
        rows.append((stamp, _usage_total(sample)))
    if len(rows) < 2:
        return None
    latest = max(stamp for stamp, _ in rows)
    selected = [row for row in rows if row[0] >= latest - window * 3600.0]
    if len(selected) < 2:
        return None
    span = max(stamp for stamp, _ in selected) - min(stamp for stamp, _ in selected)
    if span < MIN_RATE_SPAN_SEC:
        return None
    return sum(tokens for _, tokens in selected) / (span / 3600.0)


def eta_hours(current_pct: float, tokens_per_pct: float,
              rate_tokens_per_hour: float) -> float | None:
    """按当前已用百分比估计额度耗尽所需小时数。

    eta = (100 − current_pct) × tokens_per_pct ÷ rate。任一输入为
    None/0/负数，或 current_pct ≥ 100（剩余额度非正）时返回 None，
    避免把哨兵值当真实结果传播。
    """
    values = [_number(value) for value in (current_pct, tokens_per_pct, rate_tokens_per_hour)]
    if any(value is None or value <= 0 for value in values):
        return None
    current, per_pct, rate = values
    remaining = 100.0 - current
    if remaining <= 0:
        return None
    return remaining * per_pct / rate


def _summarize(stat: dict[str, float], granularity_pct: float) -> dict:
    tokens, delta = stat["tokens"], stat["delta"]
    available = tokens > 0 and delta > 0
    ratio = tokens / delta if available else None
    lower = tokens / (delta + granularity_pct) if available else None
    upper = (tokens / (delta - granularity_pct)
             if available and delta > granularity_pct else None)
    return {
        "tokens_per_pct": ratio,
        "quota_tokens": ratio * 100 if ratio is not None else None,
        "q_low": lower * 100 if lower is not None else None,
        "q_high": upper * 100 if upper is not None else None,
        "n_pairs": int(stat["n_pairs"]),
        "resets": int(stat["resets"]),
        "total_tokens": int(tokens),
        "available": available,
    }


def _meta(ordered: list[dict]) -> dict:
    stamps = [stamp for stamp in (_ts(sample) for sample in ordered) if stamp is not None]
    span_hours = (max(stamps) - min(stamps)) / 3600.0 if len(stamps) >= 2 else 0.0
    external_tokens = sum(_usage_total(sample) for sample in ordered if not _clean(sample))
    return {
        "n_samples": len(ordered),
        "span_hours": span_hours,
        "external_tokens": external_tokens,
    }


def _sorted_samples(samples: list[dict]) -> list[dict]:
    items = list(samples or [])
    # 缺 ts 的样本排在最后，稳定排序保证同 ts 时保持原有先后。
    return sorted(items, key=lambda sample: (_ts(sample) is None, _ts(sample) or 0.0))


def _usage_total(sample: Any) -> int:
    if not isinstance(sample, dict):
        return 0
    usage = sample.get("usage")
    if not isinstance(usage, dict):
        return 0
    return max(0, total_tokens(usage))


def _pools(sample: Any) -> dict[str, float]:
    if not isinstance(sample, dict):
        return {}
    raw = sample.get("pools")
    if not isinstance(raw, dict):
        return {}
    pools: dict[str, float] = {}
    for name, value in raw.items():
        number = _number(value)
        if number is not None:
            pools[str(name)] = number
    return pools


def _clean(sample: Any) -> bool:
    return bool(sample.get("clean", True)) if isinstance(sample, dict) else True


def _ts(sample: Any) -> float | None:
    return _number(sample.get("ts")) if isinstance(sample, dict) else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
