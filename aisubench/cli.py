from __future__ import annotations

import argparse
import json
import sys

from .calibrate import calibrate
from .config import ROOT, load_config
from .gui import run_gui
from .quota.arkcli import ArkcliQuotaProbe
from .quota.manual import ManualQuotaProbe
from .quota.mock import MockQuotaProbe
from .report import write_report
from .runner import Runner
from .status import run_status
from .task import list_tasks, load_task
from .watch import run_watch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aisubench", description="AI 订阅评测框架")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出内置任务")
    run = sub.add_parser("run", help="运行一个任务")
    run.add_argument("task")
    run.add_argument("--agent", default="mock")
    batch = sub.add_parser("batch", help="批量运行任务")
    batch.add_argument("--tier", choices=["calibration", "light", "medium"], required=True)
    batch.add_argument("--agent", default="mock")
    cal = sub.add_parser("calibrate", help="标定订阅额度")
    cal.add_argument("--plan", default="demo")
    cal.add_argument("--probe", choices=["mock", "arkcli", "manual"], default="mock")
    cal.add_argument("--agent", default="mock")
    report = sub.add_parser("report", help="生成 Markdown 报告")
    report.add_argument("--out", default="reports/report.md")
    report.add_argument("--agent")
    report.add_argument("--last", type=int)
    report.add_argument("--price", type=float)
    report.add_argument("--quota-tokens", type=float)
    watch = sub.add_parser("watch", help="持续监测采样：meter 增量 + 额度探针快照写入账本")
    watch.add_argument("--agent", required=True,
                       help="agent 名，取 [agents.*] 的 meter/log_path（mock 用内置合成日志）")
    watch.add_argument("--probe", required=True, choices=["mock", "arkcli", "manual"],
                       help="额度探针")
    watch.add_argument("--once", action="store_true", help="只采样一次即退出（适合 cron）")
    watch.add_argument("--interval", type=float,
                       help="循环采样间隔秒数（默认取 [watch].interval_sec）")
    watch.add_argument("--ledger", help="账本 JSONL 路径（默认 state/ledger.jsonl）")
    status = sub.add_parser("status", help="持续监测状态：读账本输出各池 当前%%/Q 估计/速率/ETA")
    status.add_argument("--ledger", help="账本 JSONL 路径（默认取 [watch].ledger）")
    status.add_argument("--window-hours", dest="window_hours", type=float,
                        help="消耗速率窗口小时数（默认 24，右端=该池最新样本）")
    status.add_argument("--clean-only", dest="clean_only", action="store_true",
                        help="只用 clean 样本估计与计速率，排除未观测渠道污染")
    gui = sub.add_parser("gui", help="本地 Web 监控面板（只绑 127.0.0.1，可选进程内采样）")
    gui.add_argument("--port", type=int, default=7788, help="监听端口（默认 7788）")
    gui.add_argument("--window-hours", dest="window_hours", type=float,
                     help="消耗速率窗口小时数（默认 24，同 status）")
    gui.add_argument("--clean-only", dest="clean_only", action="store_true",
                     help="只用 clean 样本估计与计速率（同 status）")
    gui.add_argument("--sample-interval", dest="sample_interval", type=float, metavar="SEC",
                     help="GUI 进程内后台采样间隔秒数（需同时给 --agent/--probe；不传则只读账本）")
    gui.add_argument("--agent",
                     help="agent 名，取 [agents.*] 的 meter/log_path（mock 用内置合成日志）")
    gui.add_argument("--probe", choices=["mock", "arkcli", "manual"], help="额度探针")
    gui.add_argument("--ledger", help="账本 JSONL 路径（默认取 [watch].ledger）")
    gui.add_argument("--debug", action="store_true",
                     help="开启调试模式：主面板显示入口，GET /debug 展示状态栏预览与原始数据")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(argv)
        if args.command == "list":
            for task in list_tasks():
                print(f"{task.id}\t{task.tier}\t{task.timeout_sec:g}s\t{task.prompt}")
            return 0
        if args.command == "run":
            task = load_task(args.task)
            result = Runner().run(task, args.agent)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["converged"] else 1
        if args.command == "batch":
            tasks = list_tasks(tier=args.tier)
            if not tasks:
                print(f"错误：档位 {args.tier} 没有任务", file=sys.stderr)
                return 1
            results = [Runner().run(task, args.agent) for task in tasks]
            passed = sum(result["converged"] for result in results)
            false_convergence = sum(result.get("false_convergence", False) for result in results)
            print(f"{args.tier}：{passed}/{len(results)} 个任务通过；假收敛：{false_convergence}")
            return 0 if passed == len(results) else 1
        if args.command == "report":
            kwargs = {"agent": args.agent, "last": args.last}
            try:
                path = write_report(ROOT / "runs", args.out, args.price, args.quota_tokens, **kwargs)
            except TypeError:
                path = write_report(ROOT / "runs", args.out, args.price, args.quota_tokens)
            print(f"报告已生成：{path}")
            return 0
        if args.command == "calibrate":
            config = load_config()
            calibration = config.get("calibration", {})
            plans = config.get("subscriptions", {})
            if args.plan not in plans and args.plan != "demo":
                print(f"错误：未找到订阅方案: {args.plan}", file=sys.stderr)
                return 1
            probes = {"mock": MockQuotaProbe, "arkcli": ArkcliQuotaProbe, "manual": ManualQuotaProbe}
            result = calibrate(
                probes[args.probe](),
                max_tasks=int(calibration.get("max_tasks", 20)),
                granularity_pct=float(calibration.get("granularity_pct", 1.0)),
                agent=args.agent,
            )
            result["plan"] = args.plan
            reports_dir = ROOT / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            safe_plan = "".join(char if char.isalnum() or char in "-_" else "_" for char in args.plan)
            (reports_dir / f"calibration-{safe_plan}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "watch":
            return run_watch(args.agent, args.probe, once=args.once,
                             interval=args.interval, ledger=args.ledger)
        if args.command == "status":
            return run_status(ledger=args.ledger, window_hours=args.window_hours,
                              clean_only=args.clean_only)
        if args.command == "gui":
            return run_gui(port=args.port, window_hours=args.window_hours,
                           clean_only=args.clean_only,
                           sample_interval=args.sample_interval,
                           agent=args.agent, probe=args.probe, ledger=args.ledger,
                           debug=args.debug)
        return 2
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
