# AISUBench

AI 订阅服务评测框架（AI Subscription Benchmark）。它用确定性任务、任务验收、token 计量和额度标定，比较不同 AI 订阅的实际有效产出与成本。

## 快速开始

项目只依赖 Python 3.12 标准库，无需安装即可在仓库根目录运行：

```bash
python3 -m aisubench list
python3 -m aisubench batch --tier calibration --agent mock
python3 -m aisubench batch --tier light --agent mock
python3 -m aisubench calibrate --plan demo --probe mock
python3 -m aisubench report
```

mock agent 完全离线、确定性地产生任务产物和假 usage，可用于验证完整链路。真实 agent 的命令模板写在 `aisubench.toml`，私有覆盖写在被 git 忽略的 `aisubench.local.toml`，不要把 API key 或其他凭证写入仓库。

常用命令：

```text
python3 -m aisubench run <任务 id> --agent mock
python3 -m aisubench batch --tier light --agent mock
python3 -m aisubench calibrate --plan <名称> --probe mock|arkcli|manual
python3 -m aisubench report --out reports/report.md
python3 -m aisubench report --agent mock --last 10
python3 -m aisubench report --price 30 --quota-tokens 8200
```

`aisubench report` 参数说明：

- `--out PATH`：报告输出路径，默认 `reports/report.md`。
- `--agent NAME`：只汇总该 agent 的 run；缺省时汇总 runs/ 下全部 run，并在报告顶部注明。
- `--last N`：按 runs/ 目录修改时间从新到旧只取最新 N 个 run。
- `--price YUAN` / `--quota-tokens N`：订阅折算用的价格与 100% 额度 token 当量。两者缺省时自动取 `aisubench.toml` 的 `[subscriptions.*]` 价格 + 最新 `reports/calibration-*.json` 标定出的 Q；都没有时报告省略元/task 行。

报告中标定区间按 ±g 读数误差传播（g 为百分比显示粒度），`Δ ≤ g` 的窗口上界标记为"不可用"；tokens=0 但额度有变化的窗口同样记为"不可用"。

## 仓库结构

```text
aisubench/                 扁平 Python 包与 CLI
  cli.py                   list/run/batch/calibrate/report 命令入口
  task.py runner.py        任务加载、隔离执行和验收
  verify.py                验收脚本进程管理（独立进程组、超时击杀）
  calibrate.py             订阅额度标定（±g 置信区间）
  metrics.py               token/TPS/成本指标口径
  report.py                Markdown 报告生成（过滤、折算、标定展示）
  config.py                aisubench.toml / aisubench.local.toml 加载
  meters/                  mock、API、Kimi、Claude 计量适配器
  quota/                   mock、ArkCLI、人工额度探针
tasks/                     10 个确定性任务及 fixtures/verify.py/solution
runs/                      被 git 忽略的运行 JSON 产物
reports/                   被 git 忽略的标定 JSON 与 Markdown 报告
tests/                     unittest 测试
docs/benchmark-design.md   架构与标定方法
docs/metrics.md            精确指标口径
aisubench.toml             默认命令、价格和标定参数（示例/合成数据，非真实价格）
pyproject.toml             未来安装用的包元数据
```

完整的指标公式见 [docs/metrics.md](docs/metrics.md)，架构与适配器协议见 [docs/benchmark-design.md](docs/benchmark-design.md)。

## 测试

```bash
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
