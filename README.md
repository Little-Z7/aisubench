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
python3 -m aisubench watch --agent mock --probe mock --once
python3 -m aisubench status
python3 -m aisubench gui --agent mock --probe mock --sample-interval 60
```

最后一条会启动本地监控面板（只绑 127.0.0.1，默认 <http://127.0.0.1:7788>），浏览器里每 5 秒自动刷新各池用量卡片；`--sample-interval` 让 GUI 进程内周期采样，不传则只读账本。

mock agent 完全离线、确定性地产生任务产物和假 usage，可用于验证完整链路。真实 agent 的命令模板写在 `aisubench.toml`，私有覆盖写在被 git 忽略的 `aisubench.local.toml`，不要把 API key 或其他凭证写入仓库。

`watch` 是持续监测的采样侧：一次采样 = meter token 增量 + 额度探针快照，追加到被 git 忽略的账本 `state/ledger.jsonl`（真实使用中长期跑，可配 cron `--once`）；`status` 是展示侧：读账本输出各池「当前已用% | ≈tokens | 100% 当量 Q（±g 区间）| 消耗速率 | 预计耗尽」的中文监控表。账本没有样本时 `status` 会提示先跑 `watch`，这不是错误（退出码仍为 0）。

常用命令：

```text
python3 -m aisubench run <任务 id> --agent mock
python3 -m aisubench batch --tier light --agent mock
python3 -m aisubench calibrate --plan <名称> --probe mock|arkcli|manual
python3 -m aisubench report --out reports/report.md
python3 -m aisubench report --agent mock --last 10
python3 -m aisubench report --price 30 --quota-tokens 8200
python3 -m aisubench watch --agent mock --probe mock --once
python3 -m aisubench status --window-hours 24
python3 -m aisubench gui --port 7788
```

`aisubench report` 参数说明：

- `--out PATH`：报告输出路径，默认 `reports/report.md`。
- `--agent NAME`：只汇总该 agent 的 run；缺省时汇总 runs/ 下全部 run，并在报告顶部注明。
- `--last N`：按 runs/ 目录修改时间从新到旧只取最新 N 个 run。
- `--price YUAN` / `--quota-tokens N`：订阅折算用的价格与 100% 额度 token 当量。两者缺省时自动取 `aisubench.toml` 的 `[subscriptions.*]` 价格 + 最新 `reports/calibration-*.json` 标定出的 Q；都没有时报告省略元/task 行。

报告中标定区间按 ±g 读数误差传播（g 为百分比显示粒度），`Δ ≤ g` 的窗口上界标记为"不可用"；tokens=0 但额度有变化的窗口同样记为"不可用"。

`aisubench status` 参数说明：

- `--ledger PATH`：账本路径，缺省取 `aisubench.toml` 的 `[watch].ledger`（默认 `state/ledger.jsonl`）。
- `--window-hours N`：消耗速率窗口小时数，默认 24（窗口右端=该池最新样本）。
- `--clean-only`：比率估计与速率只用 `clean=true` 的样本，排除网页/手机端等未观测渠道的污染。

`[subscriptions.*].pools` 声明的池会排在表首，账本里还没出现该池样本时单独标注"账本中暂无该池样本"。

`aisubench gui` 参数说明：

- `--port N`：监听端口，默认 7788；服务只绑定 127.0.0.1，不暴露到局域网。
- `--window-hours N` / `--clean-only` / `--ledger PATH`：与 `status` 同口径同默认。
- `--agent X --probe Y`：启用页面「立即采样」按钮（复用 watch 的 meter + 探针会话）；两者需同时提供，不传则按钮置灰。
- `--sample-interval SEC`：GUI 进程内后台每 SEC 秒采样一次（需同时给 `--agent/--probe`），单进程 = 采样 + 展示；不传则只读账本。

页面数字与 `status` 完全同源（共用 `collect_status`），措辞一致（不可用 / 数据不足(Δ 未超粒度)）；最新样本超过采样间隔 3 倍时头部置灰提示数据过期。

## 原生壳（macOS 状态栏）

`shells/macos` 是纯原生 macOS 菜单栏监控应用（rumps）：状态栏常驻显示最紧急池的 `池名 已用%`，下拉菜单为与 `status` 同口径的各池数字。**无浏览器跳转、无 WebView、无本地 HTTP 服务**——取数靠进程内 `collect_status`，采样靠进程内 daemon 线程复用 `WatchSession`，不开端口。壳依赖（rumps）独立声明在 `shells/macos/requirements.txt`，`aisubench` 核心包保持零第三方依赖。

```bash
pip install -r shells/macos/requirements.txt
python3 -m shells.macos --agent mock --probe mock   # 采样+展示单进程
python3 -m shells.macos --no-sample                 # 只读账本
```

参数：`--agent/--probe`（启用采样与「立即采样」按钮，需同时提供）、`--interval`（采样间隔，默认 300s）、`--refresh`（菜单刷新，默认 30s）、`--no-sample`、`--ledger`。详见 [shells/macos/README.md](shells/macos/README.md)。

## 仓库结构

```text
aisubench/                 扁平 Python 包与 CLI
  cli.py                   list/run/batch/calibrate/report/watch/status/gui 命令入口
  task.py runner.py        任务加载、隔离执行和验收
  verify.py                验收脚本进程管理（独立进程组、超时击杀）
  calibrate.py             订阅额度标定（±g 置信区间）
  metrics.py               token/TPS/成本指标口径
  report.py                Markdown 报告生成（过滤、折算、标定展示）
  ledger.py                持续监测账本：watch 写入、status/estimate 读取的 JSONL 样本
  estimate.py              池比率估计（±g 区间）、消耗速率、ETA（只读账本）
  watch.py                 持续监测采样：meter 增量 + 探针快照落账本
  status.py                持续监测状态：collect_status 结构化结果 + 中文监控表渲染
  gui.py dashboard.html    本地 Web 监控面板（127.0.0.1，/api/status + /api/sample）
  config.py                aisubench.toml / aisubench.local.toml 加载
  meters/                  mock、API、Kimi、Claude 计量适配器
  quota/                   mock、ArkCLI、人工额度探针
shells/                    原生壳（与核心包解耦；核心包零第三方依赖）
  shared/viewmodel.py      壳共用视图模型：collect_status dict → 菜单行（纯函数）
  macos/                   macOS 状态栏壳（rumps；python3 -m shells.macos）
tasks/                     10 个确定性任务及 fixtures/verify.py/solution
runs/                      被 git 忽略的运行 JSON 产物
reports/                   被 git 忽略的标定 JSON 与 Markdown 报告
state/                     被 git 忽略的本地状态（账本、meter 偏移、mock 合成日志）
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
