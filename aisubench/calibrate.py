from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .config import ROOT
from .metrics import total_tokens
from .runner import Runner
from .task import list_tasks


@dataclass
class CalibrationWindow:
    name: str
    delta_pct: float
    tokens: int
    tokens_per_pct: float | None
    quota_tokens: float | None
    lower: float | None
    upper: float | None
    q_lower: float | None
    q_upper: float | None
    available: bool

    def to_dict(self) -> dict:
        return asdict(self)


def calculate_calibration(before: dict[str, float], after: dict[str, float], tokens: int,
                          granularity_pct: float = 1.0) -> list[CalibrationWindow]:
    """按 ±g 读数误差传播计算每个窗口的 tokens/% 区间。

    Δ 是 before/after 两次量化读数之差，最坏误差 ±g：
    R_low = S / (Δ + g)，R_high = S / (Δ − g)；Δ ≤ g 时上界不可用。
    tokens=0 或 Δ≤0 的窗口不可用，不输出伪有效的 0 值。
    """
    if tokens < 0 or granularity_pct <= 0:
        raise ValueError("tokens 不能为负数，granularity_pct 必须为正数")
    result = []
    for name in sorted(set(before) | set(after)):
        delta = float(after.get(name, 0)) - float(before.get(name, 0))
        if delta <= 0:
            continue
        if tokens <= 0:
            result.append(CalibrationWindow(name, delta, tokens, None, None,
                                            None, None, None, None, False))
            continue
        ratio = tokens / delta
        lower = tokens / (delta + granularity_pct)
        upper = tokens / (delta - granularity_pct) if delta > granularity_pct else None
        result.append(CalibrationWindow(name, delta, tokens, ratio, ratio * 100,
                                        lower, upper,
                                        lower * 100 if lower is not None else None,
                                        upper * 100 if upper is not None else None,
                                        True))
    return result


def calibrate(probe, runner: Runner | None = None, max_tasks: int = 20,
              granularity_pct: float = 1.0, agent: str = "mock",
              progress: Callable[[int, int], None] | None = None) -> dict:
    if max_tasks <= 0:
        raise ValueError("max_tasks 必须为正数")
    runner = runner or Runner()
    continuous = getattr(probe, "continuous", True)
    before = probe.snapshot()
    tasks = list_tasks(tier="calibration")
    results = []
    after = dict(before)
    task_cycle = (tasks * ((max_tasks + len(tasks) - 1) // len(tasks)))[:max_tasks] if tasks else []
    for index, task in enumerate(task_cycle):
        results.append(runner.run(task, agent=agent))
        if progress is not None:
            progress(len(results), len(task_cycle))
        # manual 探针读数精度有限，只在首尾各快照一次；连续探针每轮采样。
        if continuous or index == len(task_cycle) - 1:
            after = probe.snapshot()
            active = set(after)
            if active and all((after[name] - before.get(name, 0)) > granularity_pct
                              for name in active):
                break
    if not results:
        after = probe.snapshot()
    tokens = sum(total_tokens(item.get("usage", {})) for item in results)
    windows = calculate_calibration(before, after, tokens, granularity_pct)
    if not windows:
        print(f"警告：标定结束没有任何窗口额度变化（Δ>0，before={before}，after={after}），"
              f"无法得出 token 当量。请检查探针读数或增加标定任务数。")
    return {
        "before": before,
        "after": after,
        "tasks": len(results),
        "task_ids": [item.get("task_id") for item in results],
        "tokens": tokens,
        "granularity_pct": granularity_pct,
        "windows": [window.to_dict() for window in windows],
    }


def calibration_report_path(plan: str, reports_dir: str | Path | None = None) -> Path:
    """订阅标定结果文件路径：``reports/calibration-<plan>.json``（非安全字符转 _）。"""
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(plan))
    base = Path(reports_dir) if reports_dir else ROOT / "reports"
    return base / f"calibration-{safe}.json"


def calibrate_plan(config: dict, plan: str, probe, *, agent: str | None = None,
                   runner: Runner | None = None, reports_dir: str | Path | None = None,
                   progress: Callable[[int, int], None] | None = None) -> dict:
    """按订阅跑一次标定并落盘：复用 ``calibrate`` 的数学，写 ``calibration-<plan>.json``。

    ``agent`` 缺省时取 ``[subscriptions.<plan>].agent``；仍为空则 ValueError。
    任务上限与读数粒度取 ``[calibration]`` 的 max_tasks / granularity_pct。
    """
    calibration = config.get("calibration") or {}
    sub_cfg = (config.get("subscriptions") or {}).get(plan) or {}
    agent = str(agent or sub_cfg.get("agent") or "").strip()
    if not agent:
        raise ValueError(f"订阅 {plan} 未绑定 agent，无法标定")
    result = calibrate(
        probe, runner=runner,
        max_tasks=int(calibration.get("max_tasks", 20)),
        granularity_pct=float(calibration.get("granularity_pct", 1.0)),
        agent=agent, progress=progress)
    result["plan"] = plan
    path = calibration_report_path(plan, reports_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return result
