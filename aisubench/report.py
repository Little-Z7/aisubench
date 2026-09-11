from __future__ import annotations

import json
from pathlib import Path

from .config import load_config
from .metrics import task_metrics, total_tokens, tps_gen, tps_wall


def load_results(runs_dir: str | Path, agent: str | None = None,
                 last: int | None = None) -> list[dict]:
    """加载 runs/ 下的 result.json，按目录 mtime 从新到旧排序。

    agent 过滤按结果里的 agent 字段；last 只保留最新 N 个。两个参数都缺省时
    返回全部 run，报告顶部会注明汇总范围。
    """
    paths = sorted(Path(runs_dir).glob("*/result.json"),
                   key=lambda path: path.parent.stat().st_mtime, reverse=True)
    results = []
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if agent is not None and item.get("agent") != agent:
            continue
        results.append(item)
    if last is not None and last >= 0:
        results = results[:last]
    return results


def load_calibrations(reports_dir: str | Path) -> list[dict]:
    calibrations = []
    for path in sorted(Path(reports_dir).glob("calibration-*.json"),
                       key=lambda path: path.stat().st_mtime):
        try:
            calibrations.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return calibrations


def render_report(results: list[dict], title: str = "AISUBench 评测报告",
                  price: float | None = None, quota_tokens: float | None = None,
                  calibrations: list[dict] | None = None) -> str:
    summary = task_metrics(results, price, quota_tokens)
    lines = [f"# {title}"]
    if price is None and quota_tokens is None:
        lines.append("")
        lines.append("> 本次运行未提供 --price / --quota-tokens，订阅折算行省略。")
    lines.extend(["", "## 汇总", "",
                  f"- 任务数：{summary['tasks']}",
                  f"- 通过数：{summary['passed']}",
                  f"- 通过率：{_metric_text(summary['pass_rate'], '{:.1%}')}",
                  f"- 总 token：{summary['total_tokens']}",
                  f"- tokens/task（全部）：{_metric_text(summary['tokens_per_task'], '{:.1f}')}",
                  f"- tokens/task（仅通过）：{_metric_text(summary['tokens_per_passed_task'], '{:.1f}')}"])
    if "yuan_per_task" in summary:
        lines.extend([f"- 元/task：{_metric_text(summary['yuan_per_task'], '{:.4f}')}",
                      f"- 元/有效任务：{_metric_text(summary['yuan_per_effective_task'], '{:.4f}')}"])
    lines.extend(["", "## 任务明细", "",
                  "| 任务 | Agent | 结果 | tokens | cached | TPS_gen | TPS_wall |",
                  "|---|---|---:|---:|---:|---:|---:|"])
    for item in results:
        usage = item.get("usage", {})
        result = "通过" if item.get("converged", item.get("verify", {}).get("passed", False)) else "失败"
        lines.append(
            f"| {item.get('task_id', '')} | {item.get('agent', '')} | {result} | "
            f"{total_tokens(usage)} | {usage.get('cached', 0)} | "
            f"{tps_gen(usage):.2f} | {tps_wall(usage, item.get('wall_time')):.2f} |"
        )
    if calibrations:
        lines.extend(["", "## 额度标定", "",
                      "| 方案 | 任务数 | token | 窗口 | Δ% | tokens/% | 100% token 当量 | Q_low–Q_high | 区间 |",
                      "|---|---:|---:|---|---:|---:|---:|---:|---|"])
        for calibration in calibrations:
            for window in calibration.get("windows", []):
                upper = window.get("upper")
                interval = (f"{window.get('lower', 0):.1f}–{upper:.1f}"
                            if upper is not None else "不可用")
                q_lower, q_upper = window.get("q_lower"), window.get("q_upper")
                q_interval = (f"{q_lower:.0f}–{q_upper:.0f}"
                              if q_lower is not None and q_upper is not None
                              else (f"≤{q_upper:.0f}" if q_upper is not None else "不可用"))
                if not window.get("available", True):
                    ratio_text = q_text = "不可用"
                else:
                    ratio_text = f"{window.get('tokens_per_pct', 0):.2f}"
                    q_text = f"{window.get('quota_tokens', 0):.0f}"
                lines.append(
                    f"| {calibration.get('plan', '')} | {calibration.get('tasks', 0)} | "
                    f"{calibration.get('tokens', 0)} | {window.get('name', '')} | "
                    f"{window.get('delta_pct', 0):.2f} | {ratio_text} | "
                    f"{q_text} | {q_interval} | {interval} |"
                )
    return "\n".join(lines) + "\n"


def write_report(runs_dir: str | Path, output: str | Path, price: float | None = None,
                 quota_tokens: float | None = None, agent: str | None = None,
                 last: int | None = None) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results = load_results(runs_dir, agent=agent, last=last)
    calibrations = load_calibrations(output_path.parent)
    if price is None or quota_tokens is None:
        default_price, default_quota = _default_price_quota(calibrations)
        price = default_price if price is None else price
        quota_tokens = default_quota if quota_tokens is None else quota_tokens
    total_runs = len(list(Path(runs_dir).glob("*/result.json")))
    header_note = ""
    if agent is None and last is None:
        header_note = f"（汇总了 runs/ 下全部 {total_runs} 个 run）"
    report = render_report(results, title=f"AISUBench 评测报告{header_note}",
                           price=price, quota_tokens=quota_tokens,
                           calibrations=calibrations)
    output_path.write_text(report, encoding="utf-8")
    return output_path


def _default_price_quota(calibrations: list[dict]) -> tuple[float | None, float | None]:
    """--price/--quota-tokens 缺省时：价格取 aisubench.toml 的 [subscriptions.*]，
    Q 取最新一份 calibration-*.json 中第一个可用窗口的 100% token 当量。
    两者都拿不到才返回 None（省略元/task 行）。"""
    price = None
    try:
        subscriptions = load_config().get("subscriptions", {})
        price = next(iter(subscriptions.values()), {}).get("price")
    except (OSError, ValueError, TypeError, KeyError):
        price = None
    quota = None
    for calibration in reversed(calibrations):
        for window in calibration.get("windows", []):
            if window.get("available", True) and window.get("quota_tokens"):
                quota = float(window["quota_tokens"])
                break
        if quota is not None:
            break
    if price is None or quota is None:
        return None, None
    return float(price), quota


def _metric_text(value: float | None, fmt: str) -> str:
    return "不可用" if value is None else fmt.format(value)
