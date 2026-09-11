"""status 报告：读账本 + 配置，把 ``estimate`` 的池估计渲染成中文监控表。

    aisubench status [--ledger PATH] [--window-hours N] [--clean-only]

- 数据源只读：``ledger.load_samples`` + ``estimate.estimate_pools`` /
  ``burn_rate`` / ``eta_hours``，status 不写账本、不采样。
- 池集合：配置 ``[subscriptions.*].pools`` 在前、账本样本实际出现的池在后；
  配置声明但样本里没有的池按「暂无该池样本」行展示。
- 消耗速率窗口取 ``--window-hours``（默认 24 小时，右端=该池最新样本）；
  ``--clean-only`` 时比率估计与速率都只用 clean 样本，排除网页/手机端等
  未观测渠道的污染。
- 退出码恒为 0（含账本不存在/为空的正常初始态，打印 watch 引导提示）。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from .config import ROOT, load_config
from .estimate import burn_rate, estimate_pools, eta_hours
from .ledger import DEFAULT_LEDGER, load_samples

DEFAULT_WINDOW_HOURS = 24.0
# 有效对数低于该值时提示估计仅供参考（单个 Δ 的 ±g 量化误差占比过大）。
LOW_PAIRS_HINT_MAX = 3

_UNAVAILABLE = "不可用"
_INSUFFICIENT = "数据不足(Δ 未超粒度)"


def run_status(ledger: str | Path | None = None, window_hours: float | None = None,
               clean_only: bool = False, config: dict | None = None) -> int:
    """status 子命令主体。``config`` 可注入，便于测试；返回退出码（恒 0）。"""
    config = load_config() if config is None else config
    print(render_status(collect_status(config, ledger, window_hours, clean_only)))
    return 0


def collect_status(config: dict, ledger: str | Path | None = None,
                   window_hours: float | None = None, clean_only: bool = False) -> dict:
    """把 status 的全部计算产出为结构化 dict：``render_status`` 与 GUI 共用。

    返回 {ledger_path, ledger_exists, window_hours, clean_only, n_samples,
    span_hours, external_tokens, last_sample_ts, pools}；``pools`` 每项为
    {name, has_samples, current_pct, used_tokens, tokens_per_pct, available,
    quota_tokens, q_low, q_high, n_pairs, resets, rate_tph, eta_hours}，
    数值缺失一律为 None（不可用），不输出伪 0 值。
    """
    watch_cfg = config.get("watch") or {}
    ledger_path = _resolve_path(ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)
    window = DEFAULT_WINDOW_HOURS if window_hours is None else float(window_hours)
    if window <= 0:
        raise ValueError("--window-hours 必须为正数")
    samples = load_samples(ledger_path)
    estimates = estimate_pools(samples, granularity_pct=_granularity(config),
                               clean_only=clean_only)
    meta = estimates.get("_meta") or {}
    latest = _latest_pools(samples)
    seen = sorted(name for name in estimates if name != "_meta")
    ordered = list(dict.fromkeys(list(_configured_pools(config)) + seen))
    rate_samples = ([s for s in samples if _is_clean(s)] if clean_only
                    else samples) if samples else []
    stamps = [stamp for stamp in (_num(s.get("ts")) for s in samples)
              if stamp is not None]
    return {
        "ledger_path": str(ledger_path),
        "ledger_exists": ledger_path.exists(),
        "window_hours": window,
        "clean_only": bool(clean_only),
        "n_samples": int(meta.get("n_samples", len(samples))),
        "span_hours": float(meta.get("span_hours", 0.0)),
        "external_tokens": int(meta.get("external_tokens", 0) or 0),
        "last_sample_ts": max(stamps) if stamps else None,
        "pools": [_collect_pool(name, estimates.get(name), latest.get(name),
                                rate_samples, window) for name in ordered],
    }


def _collect_pool(name: str, est: dict | None, current_pct: float | None,
                  rate_samples: list[dict], window_hours: float) -> dict:
    """单池的结构化结果：当前% / 已用 tokens / Q 区间 / 速率 / ETA / 对数与重置。"""
    ratio = est.get("tokens_per_pct") if est else None
    rate = burn_rate(rate_samples, name, window_hours)
    eta = eta_hours(current_pct, ratio, rate) if rate is not None else None
    return {
        "name": name,
        "has_samples": est is not None,
        "current_pct": current_pct,
        "used_tokens": (current_pct * ratio
                        if current_pct is not None and ratio is not None else None),
        "tokens_per_pct": ratio,
        "available": bool(est and est.get("available")),
        "quota_tokens": est.get("quota_tokens") if est else None,
        "q_low": est.get("q_low") if est else None,
        "q_high": est.get("q_high") if est else None,
        "n_pairs": est.get("n_pairs") if est else None,
        "resets": est.get("resets") if est else None,
        "rate_tph": rate,
        "eta_hours": eta,
    }


def render_status(status: dict) -> str:
    """把 ``collect_status`` 的结构化结果渲染成中文报告文本（不做 IO，便于单测）。"""
    lines = ["# AISUBench 持续监测状态", f"账本：{status['ledger_path']}"]
    if status["n_samples"] == 0:
        suffix = "" if status["ledger_exists"] else "（文件尚不存在）"
        lines.append(f"账本{suffix}还没有样本——这是正常的初始状态。")
        lines.append("先跑一次采样，再看这里：")
        lines.append("  aisubench watch --agent <name> --probe mock --once")
        return "\n".join(lines)

    window_hours = status["window_hours"]
    mode = "仅 clean 样本" if status["clean_only"] else "全部样本"
    lines.append(f"- 样本数：{status['n_samples']}（速率窗口 {window_hours:g} 小时；估计用{mode}）")
    lines.append(f"- 监测跨度：{status['span_hours']:.1f} 小时")
    external = status["external_tokens"]
    if external > 0:
        lines.append(f"- 提示：clean=False 区间累计约 {external:,} tokens 可能混入未观测渠道"
                     "（网页版、手机端等）；加 --clean-only 可将其排除出估计。")

    pools = status["pools"]
    if not pools:
        lines.append("")
        lines.append("样本里没有任何额度池，且配置 [subscriptions.*].pools 未声明池。")
        return "\n".join(lines)

    rows = [_pool_row(pool) for pool in pools]
    header = ("池", "当前已用%", "已用≈tokens", f"100%当量Q(低–高)",
              f"速率(近{window_hours:g}h)", "预计耗尽", "备注")
    lines.append("")
    lines.extend(_render_table(header, rows))
    return "\n".join(lines)


def _pool_row(pool: dict) -> tuple[str, ...]:
    used = "—" if pool["used_tokens"] is None else f"{pool['used_tokens']:,.0f}"
    if not pool["has_samples"]:
        quota = "—"
    elif not pool["available"]:
        quota = _INSUFFICIENT
    else:
        low, high = pool["q_low"], pool["q_high"]
        quota = (f"{pool['quota_tokens']:,.0f} ({low:,.0f}–{high:,.0f})"
                 if high is not None else f"{pool['quota_tokens']:,.0f} (≥{low:,.0f})")
    rate, eta = pool["rate_tph"], pool["eta_hours"]
    if not pool["has_samples"]:
        note = "账本中暂无该池样本"
    else:
        note = f"对={pool['n_pairs']} 重置={pool['resets']}"
        if pool["available"] and 0 < pool["n_pairs"] < LOW_PAIRS_HINT_MAX:
            note += "（对数偏少，估计仅供参考）"
    current_pct = pool["current_pct"]
    return (pool["name"],
            "—" if current_pct is None else f"{current_pct:g}%",
            used, quota,
            "—" if not pool["has_samples"]
            else (_UNAVAILABLE if rate is None else f"{rate:,.0f} tok/h"),
            "—" if not pool["has_samples"]
            else (_UNAVAILABLE if eta is None else f"{eta:.1f} h"),
            note)


def _latest_pools(samples: list[dict]) -> dict[str, float]:
    """各池最新一次的已用百分比（load_samples 已按 ts 升序，逐池覆盖取最后读数）。"""
    latest: dict[str, float] = {}
    for sample in samples:
        raw = sample.get("pools")
        if not isinstance(raw, dict):
            continue
        for name, value in raw.items():
            number = _num(value)
            if number is not None:
                latest[str(name)] = number
    return latest


def _configured_pools(config: dict) -> list[str]:
    """[subscriptions.*].pools 的并集，保持首次出现顺序。"""
    pools: list[str] = []
    for subscription in (config.get("subscriptions") or {}).values():
        if not isinstance(subscription, dict):
            continue
        declared = subscription.get("pools")
        if isinstance(declared, (list, tuple)):
            pools.extend(str(name) for name in declared)
    return list(dict.fromkeys(pools))


def _granularity(config: dict) -> float:
    """读数粒度 g：优先订阅级 granularity_pct，其次 [calibration]，默认 1.0。"""
    for subscription in (config.get("subscriptions") or {}).values():
        if isinstance(subscription, dict):
            value = _num(subscription.get("granularity_pct"))
            if value is not None and value > 0:
                return value
    value = _num((config.get("calibration") or {}).get("granularity_pct"))
    return value if value is not None and value > 0 else 1.0


def _render_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [_display_width(cell) for cell in header]
    for row in rows:
        widths = [max(w, _display_width(cell)) for w, cell in zip(widths, row)]
    lines = ["  ".join(_pad(h, w) for h, w in zip(header, widths)).rstrip(),
             "  ".join("-" * w for w in widths)]
    lines.extend("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip() for row in rows)
    return lines


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _is_clean(sample: Any) -> bool:
    return bool(sample.get("clean", True)) if isinstance(sample, dict) else True


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_path(value: Any, default: Path) -> Path:
    if not value:
        return Path(default)
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else ROOT / path
