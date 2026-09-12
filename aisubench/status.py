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

import re
import time
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

# 旧账本样本没有 subscription 字段时归入的分组名。
DEFAULT_SUBSCRIPTION = "default"
# 数据新鲜度判定的兜底参考间隔（[watch].interval_sec 缺省值，与 watch 一致）。
DEFAULT_INTERVAL_SEC = 300.0
# 池名解析不出窗口小时数时的兜底值。
DEFAULT_POOL_WINDOW_HOURS = 24.0

_HOURS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*h$")
_DAYS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*d$")
_NAMED_WINDOWS = {"week": 168.0, "w": 168.0, "month": 720.0, "mo": 720.0}


def run_status(ledger: str | Path | None = None, window_hours: float | None = None,
               clean_only: bool = False, config: dict | None = None) -> int:
    """status 子命令主体。``config`` 可注入，便于测试；返回退出码（恒 0）。"""
    config = load_config() if config is None else config
    print(render_status(collect_subscriptions(config, ledger, window_hours, clean_only)))
    return 0


def collect_status(config: dict, ledger: str | Path | None = None,
                   window_hours: float | None = None, clean_only: bool = False) -> dict:
    """把 status 的全部计算产出为结构化 dict：``render_status`` 与 GUI 共用。

    返回 {ledger_path, ledger_exists, window_hours, clean_only, n_samples,
    span_hours, external_tokens, last_sample_ts, pools}；``pools`` 每项为
    {name, has_samples, current_pct, prev_pct, used_tokens, tokens_per_pct,
    available, quota_tokens, q_low, q_high, n_pairs, resets, rate_tph,
    eta_hours}，数值缺失一律为 None（不可用），不输出伪 0 值。
    """
    ledger_path, window, samples = _resolve_inputs(config, ledger, window_hours)
    pools, meta = _collect_pools(config, samples, None, window, clean_only)
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
        "pools": pools,
    }


def collect_subscriptions(config: dict, ledger: str | Path | None = None,
                          window_hours: float | None = None,
                          clean_only: bool = False, now: float | None = None,
                          stale_interval_sec: float | None = None) -> dict:
    """按订阅分组的状态结果：与 ``collect_status`` 同口径，只是池按订阅拆分。

    样本的 ``subscription`` 字段决定归属；旧样本无该字段时归 ``default``。
    订阅的显示字段（label/account/agent/host）取 ``[subscriptions.<name>]``，
    未声明的订阅按账本样本兜底（agent 取最新样本的 agent、host 取 localhost）。

    返回与 ``collect_status`` 相同的全局元数据键，但 ``pools`` 换成
    ``subscriptions``：每项 {name, label, account, agent, host,
    last_sample_ts, last_sample_rel, stale, n_samples, external_tokens,
    analysis, usage_totals, pools}；``pools`` 与 ``collect_status`` 的池
    dict 同结构。``usage_totals`` 为该订阅监测跨度内样本 usage 的累计
    {input, cached, output, total, requests}（无样本时为 None）。
    顶层另有 ``usage_totals``（全部样本同口径累计）与 ``window_usage``
    （仅最近 ``window_hours`` 窗口内样本的累计，供面板头部展示）。
    ``stale`` 判定同 GUI 口径：最新样本距今超过参考间隔 3 倍（参考间隔取
    ``stale_interval_sec`` 或 ``[watch].interval_sec``，默认 300s）。
    """
    ledger_path, window, samples = _resolve_inputs(config, ledger, window_hours)
    watch_cfg = config.get("watch") or {}
    interval = _num(stale_interval_sec) \
        or _num(watch_cfg.get("interval_sec")) or DEFAULT_INTERVAL_SEC
    current_ts = time.time() if now is None else float(now)

    groups: dict[str, list[dict]] = {}
    for sample in samples:
        groups.setdefault(_subscription_of(sample), []).append(sample)

    subs_cfg = config.get("subscriptions") or {}
    names = [name for name, sub in subs_cfg.items() if isinstance(sub, dict)]
    names += sorted(name for name in groups if name not in subs_cfg)

    subscriptions: list[dict] = []
    for name in names:
        sub_samples = groups.get(name, [])
        sub_cfg = subs_cfg.get(name) or {}
        pools, meta = _collect_pools(config, sub_samples, name, window, clean_only)
        stamps = [stamp for stamp in (_num(s.get("ts")) for s in sub_samples)
                  if stamp is not None]
        last = max(stamps) if stamps else None
        analysis = " · ".join(note for note in
                              (pace_note(pool) for pool in pools) if note)
        subscriptions.append({
            "name": name,
            "label": str(sub_cfg.get("label") or name),
            "account": mask_account(sub_cfg.get("account")),
            "agent": str(sub_cfg.get("agent")
                         or _last_agent(sub_samples) or "—"),
            "host": str(sub_cfg.get("host") or "localhost"),
            "last_sample_ts": last,
            "last_sample_rel": relative_time(last, current_ts),
            "stale": bool(last is not None
                          and current_ts - last > 3 * interval),
            "n_samples": int(meta.get("n_samples", len(sub_samples))),
            "external_tokens": int(meta.get("external_tokens", 0) or 0),
            "analysis": analysis,
            "usage_totals": _usage_totals(sub_samples),
            "pools": pools,
        })

    all_stamps = [stamp for stamp in (_num(s.get("ts")) for s in samples)
                  if stamp is not None]
    last_ts = max(all_stamps) if all_stamps else None
    window_floor = (last_ts - window * 3600.0) if last_ts is not None else None
    window_samples = [s for s in samples
                      if window_floor is not None
                      and (_num(s.get("ts")) or 0.0) >= window_floor]
    _, overall_meta = _collect_pools(config, samples, None, window, clean_only)
    return {
        "ledger_path": str(ledger_path),
        "ledger_exists": ledger_path.exists(),
        "window_hours": window,
        "clean_only": bool(clean_only),
        "n_samples": int(overall_meta.get("n_samples", len(samples))),
        "span_hours": float(overall_meta.get("span_hours", 0.0)),
        "external_tokens": int(overall_meta.get("external_tokens", 0) or 0),
        "last_sample_ts": last_ts,
        "usage_totals": _usage_totals(samples),
        "window_usage": _usage_totals(window_samples),
        "subscriptions": subscriptions,
    }


def _usage_totals(samples: list[dict]) -> dict | None:
    """一组样本 usage 的累计拆分：{input, cached, output, total, requests}。

    无样本（或样本都没有 usage 字段）时返回 None，前端据此显示「暂无」
    而不是假 0；单键缺失按 0 计（与 estimate 的口径一致）。
    """
    if not samples:
        return None
    seen = False
    totals = {"input": 0, "cached": 0, "output": 0, "requests": 0}
    for sample in samples:
        usage = sample.get("usage") if isinstance(sample, dict) else None
        if not isinstance(usage, dict):
            continue
        seen = True
        for key in totals:
            value = _num(usage.get(key))
            if value is not None and value > 0:
                totals[key] += int(value)
    if not seen:
        return None
    totals["total"] = totals["input"] + totals["cached"] + totals["output"]
    return totals


def _resolve_inputs(config: dict, ledger: str | Path | None,
                    window_hours: float | None) -> tuple[Path, float, list[dict]]:
    """collect_status / collect_subscriptions 共用的输入解析：路径、窗口、样本。"""
    watch_cfg = config.get("watch") or {}
    ledger_path = _resolve_path(ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)
    window = DEFAULT_WINDOW_HOURS if window_hours is None else float(window_hours)
    if window <= 0:
        raise ValueError("--window-hours 必须为正数")
    return ledger_path, window, load_samples(ledger_path)


def _collect_pools(config: dict, samples: list[dict], subscription: str | None,
                   window: float, clean_only: bool) -> tuple[list[dict], dict]:
    """一组样本的池 dict 列表 + estimate ``_meta``（订阅粒度或全局）。"""
    estimates = estimate_pools(samples, clean_only=clean_only,
                               granularity_pct=_granularity(config, subscription))
    meta = estimates.get("_meta") or {}
    latest, previous = _pool_readings(samples)
    seen = sorted(name for name in estimates if name != "_meta")
    ordered = list(dict.fromkeys(
        list(_configured_pools(config, subscription)) + seen))
    rate_samples = ([s for s in samples if _is_clean(s)] if clean_only
                    else samples) if samples else []
    pools = [_collect_pool(name, estimates.get(name), latest.get(name),
                           rate_samples, window,
                           prev_pct=previous.get(name)) for name in ordered]
    return pools, meta


def _collect_pool(name: str, est: dict | None, current_pct: float | None,
                  rate_samples: list[dict], window_hours: float,
                  prev_pct: float | None = None) -> dict:
    """单池的结构化结果：当前% / 上读数% / 已用 tokens / Q 区间 / 速率 / ETA / 对数与重置。"""
    ratio = est.get("tokens_per_pct") if est else None
    rate = burn_rate(rate_samples, name, window_hours)
    eta = eta_hours(current_pct, ratio, rate) if rate is not None else None
    return {
        "name": name,
        "has_samples": est is not None,
        "current_pct": current_pct,
        "prev_pct": prev_pct,
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


def pool_window_hours(name: Any) -> float:
    """从池名解析窗口小时数：'5h'→5、'7d'/'week'→168、'month'→720，解析不了按 24。"""
    text = str(name or "").strip().lower()
    match = _HOURS_RE.match(text)
    if match:
        return float(match.group(1))
    match = _DAYS_RE.match(text)
    if match:
        return float(match.group(1)) * 24.0
    return _NAMED_WINDOWS.get(text, DEFAULT_POOL_WINDOW_HOURS)


def pace_note(pool: dict) -> str | None:
    """单池节奏判断：eta < 窗口×0.5 → 「偏快」；eta > 窗口×2 → 「偏慢」；否则 None。"""
    if not pool.get("has_samples"):
        return None
    eta = _num(pool.get("eta_hours"))
    if eta is None:
        return None
    name = str(pool.get("name") or "?")
    window = pool_window_hours(name)
    if eta < window * 0.5:
        return f"{name} 用量进度偏快"
    if eta > window * 2:
        return f"{name} 用量进度偏慢"
    return None


def relative_time(ts: float | None, now: float) -> str:
    """中文相对时间：'刚刚' / 'N 分钟前' / 'N 小时前' / 'N 天前'；None → '无样本'。"""
    stamp = _num(ts)
    if stamp is None:
        return "无样本"
    delta = max(0.0, float(now) - stamp)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def mask_account(value: Any) -> str | None:
    """账号掩码：'test@gmail.com' → 't***@g***.com'；已含 '*' 或为空时原样/None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "*" in text:
        return text
    if "@" in text:
        user, _, domain = text.partition("@")
        parts = domain.split(".")
        masked_domain = (parts[0][:1] or "*") + "***"
        if len(parts) > 1:
            masked_domain += "." + ".".join(parts[1:])
        return f"{user[:1] or '*'}***@{masked_domain}"
    return text[:1] + "***"


def _subscription_of(sample: Any) -> str:
    """样本的订阅归属：``subscription`` 字段，缺失/空 → ``default``。"""
    if not isinstance(sample, dict):
        return DEFAULT_SUBSCRIPTION
    value = sample.get("subscription")
    text = str(value).strip() if value is not None else ""
    return text or DEFAULT_SUBSCRIPTION


def _last_agent(samples: list[dict]) -> str | None:
    """该分组最新样本的 agent 名（未声明 [subscriptions.*].agent 时的兜底）。"""
    for sample in reversed(samples):
        agent = sample.get("agent")
        if agent:
            return str(agent)
    return None


def render_status(status: dict) -> str:
    """把 ``collect_subscriptions`` 的结构化结果按订阅分组渲染成中文报告文本。"""
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

    subscriptions = status.get("subscriptions") or []
    if not subscriptions:
        lines.append("")
        lines.append("样本里没有任何额度池，且配置 [subscriptions.*].pools 未声明池。")
        return "\n".join(lines)

    header = ("池", "当前已用%", "已用≈tokens", f"100%当量Q(低–高)",
              f"速率(近{window_hours:g}h)", "预计耗尽", "备注")
    for sub in subscriptions:
        lines.append("")
        label = str(sub.get("label") or sub["name"])
        account = sub.get("account")
        lines.append(f"## {label}" + (f"（{account}）" if account else ""))
        source = (f"   来源：{sub.get('agent') or '—'} · {sub.get('host') or 'localhost'}"
                  f"　最后采样：{sub.get('last_sample_rel') or '无样本'}")
        if sub.get("stale"):
            source += "（数据可能已过期）"
        lines.append(source)
        if sub.get("analysis"):
            lines.append(f"   分析：{sub['analysis']}")
        rows = [_pool_row(pool) for pool in sub.get("pools") or []]
        if not rows:
            lines.append("   该订阅暂无可展示的额度池。")
            continue
        lines.extend("   " + line for line in _render_table(header, rows))
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


def _pool_readings(samples: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """各池 (最新读数, 上一次读数)——pct 回落即额度重置/恢复的信号。"""
    latest: dict[str, float] = {}
    previous: dict[str, float] = {}
    for sample in samples:
        raw = sample.get("pools")
        if not isinstance(raw, dict):
            continue
        for name, value in raw.items():
            number = _num(value)
            if number is not None:
                key = str(name)
                if key in latest:
                    previous[key] = latest[key]
                latest[key] = number
    return latest, previous


def _configured_pools(config: dict, subscription: str | None = None) -> list[str]:
    """[subscriptions.*].pools：``subscription`` 为 None 时取全部订阅的并集。"""
    subs = config.get("subscriptions") or {}
    items = subs.items() if subscription is None else [(subscription, subs.get(subscription))]
    pools: list[str] = []
    for _, sub in items:
        if not isinstance(sub, dict):
            continue
        declared = sub.get("pools")
        if isinstance(declared, (list, tuple)):
            pools.extend(str(name) for name in declared)
    return list(dict.fromkeys(pools))


def _granularity(config: dict, subscription: str | None = None) -> float:
    """读数粒度 g：优先订阅级 granularity_pct，其次 [calibration]，默认 1.0。"""
    subs = config.get("subscriptions") or {}
    if subscription is not None:
        candidates = [subs.get(subscription)]
    else:
        candidates = list(subs.values())
    for sub in candidates:
        if isinstance(sub, dict):
            value = _num(sub.get("granularity_pct"))
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
