# AISUBench 基准设计

## 架构

AISUBench 由任务、runner、meter、quota probe、指标和报告组成，另有持续监测链路的账本（ledger）、估计器（estimate）、采样器（watch）和展示命令（status）。任务目录包含 `task.toml`、`fixtures/` 和验收脚本。runner 将 fixtures 复制到临时工作目录，以该目录为当前目录启动被测 agent，agent 退出或超时后执行验收脚本。验收脚本退出码为 0 即表示任务收敛；agent 自报完成但验收失败会记录为假收敛。

所有运行结果写为 JSON，报告只消费标准化结果，因此实际 agent 和订阅适配器可以独立演进。

## 适配器协议

meter 统一产生 `Usage`：`input`、`output`、`cached`、`requests`、`first_token_ts`、`last_token_ts` 和 `wall_time`。API agent 直接累加 API 返回的 usage；Kimi 与 Claude meter 从会话 JSONL 的运行前偏移量开始读取增量；mock meter 不联网并提供确定性数据。

被测 agent 与 runner 之间的 usage 上报契约：agent 在工作目录写 `.aisubench_usage.json`（Usage 字段的 JSON 对象），runner 在 agent 退出后读取它作为该次运行的 usage。agent 还应写 `.agent_complete` 哨兵文件表示自报完成，runner 用它与验收结果对照检测假收敛。

runner 不把任务的 `verify.py` 复制进工作区：验收脚本在仓库任务目录中解析后，直接以工作区为 cwd 执行，避免被测 agent 读取或篡改验收逻辑。mock agent 通过回放 `tasks/<id>/solution/` 下的 golden 产物通过验收，不联网。

quota probe 提供 `snapshot() -> {窗口名: 已用百分比}`，并用 `continuous` 类属性声明读数特性：mock 与 arkcli 为 `True`（可随时采样、标定循环每轮任务后快照），manual 为 `False`（读数成本高、精度有限，标定只在首尾各快照一次，输入百分比按粒度取整）。

## 标定方法

先取快照，运行标定档任务，再取快照。对每个窗口计算：

`tokens_per_quota_pct = Σ任务总 token / (after_pct - before_pct)`

窗口的 100% token 当量为该值乘 100。若百分比粒度较粗，程序会继续累计任务，直到全部活跃窗口（after 中仍存在的窗口）的变化都超过粒度（Δ > g），或达到 `max_tasks` 预算上限；多个窗口分别计算。标定结束时若没有任何窗口 Δ>0，会打印明确警告。

## 粒度与置信度

显示百分比存在量化误差。若显示粒度为 `g`，`before` 与 `after` 两次读数各自的量化误差为 ±g/2，观测变化 `Δ` 是两次读数之差，最坏误差相加为 **±g**，即 `Δ` 的真实值近似位于 `[Δ-g, Δ+g]`。因此 token/% 区间为 `[tokens/(Δ+g), tokens/(Δ-g)]`；当 `Δ - g ≤ 0`（即 `Δ ≤ g`）时上界不可用，应报告不可分辨，而不应伪造精度。可分辨判据统一为 `Δ > g`。该区间只描述显示粒度误差，不包含任务 token 估算、服务端异步扣量或窗口并发变化等系统误差。

mock 流程只用于验证框架和报告链路；真实订阅评测需要记录服务端时间、窗口定义和采样时刻，并避免并发请求污染标定。

## 持续监测（watch/status）

标定是一次性实验；持续监测则在真实使用中长期采样，链路如下：

```text
meter（token 增量） + quota probe（池%快照）
        │ watch 采样
        ▼
ledger.jsonl（账本，state/ 内，git 忽略）
        │ estimate（比率估计 / 速率 / ETA）      estimate_pools · burn_rate · eta_hours
        ▼
status.collect_status（结构化 dict）──► status（中文监控表）/ gui（本地 Web 面板）
```

- **账本契约**（`aisubench/ledger.py`，每行一个 JSON 样本）：`{"ts", "agent", "source", "usage": {input, cached, output, requests}, "pools": {池名: 已用%}, "clean"}`。`usage` 是自该 agent 上一个样本以来的 token 增量，`pools` 是采样时刻各额度窗口的已用百分比读数。
- **clean 假设**：距同 agent 上一个样本的间隔小于 `[watch].clean_window_sec`（默认 600 秒）时记 `clean=true`——假设足够密的窗口内，网页版/手机端等未被 meter 观测的渠道消耗可忽略；间隔过长则无法排除这类外部消耗，整段区间标为不 clean。首个样本没有可比的前后间隔，保守记 `clean=false`。估计侧可用 `clean_only` 丢弃任一端不 clean 的相邻对。
- **比率估计**（`aisubench/estimate.py`）：对相邻样本对 (s[i], s[i+1])，池增量 `Δ = s[i+1].pools[p] − s[i].pools[p]`，对应消耗取 `s[i+1].usage`。点估计为比率估计 `R = Σtokens / ΣΔ`（只在 Δ>0 的有效对上累加，不是各对比率的平均），池 100% 当量 `Q = 100×R`。
- **±g 置信区间**沿用标定的读数误差口径：每次量化读数误差 ±g/2，Δ 最坏误差 ±g，故 `Q_low = 100×Σtokens/(ΣΔ+g)`、`Q_high = 100×Σtokens/(ΣΔ−g)`；`ΣΔ ≤ g` 时上界不可用。样本不足以给出比率（无有效对或 tokens=0）时标为不可用，不输出伪 0 值。
- **重置检测**：`Δ < 0` 说明窗口滚动/周期重置，该对不参与比率估计，只累计 `resets` 次数并在 status 备注列展示。
- **消耗速率与 ETA**：`burn_rate` 取窗口（`--window-hours`，默认 24 小时）内含该池样本的 token 增量之和 ÷ 实际时间跨度，右端为该池最新样本；`ETA = (100 − 当前%) × R ÷ 速率`，任一输入缺失或池已用满时为不可用。

### 本地监控面板（gui）

`aisubench gui` 用标准库 `http.server.ThreadingHTTPServer` 起只绑 127.0.0.1 的单页面板（`aisubench/dashboard.html`，无外部资源，前端每 5 秒拉一次 `GET /api/status`）。`/api/status` 与 CLI `status` 共用 `collect_status`，数字与措辞（不可用 / 数据不足(Δ 未超粒度)）完全一致，另附 `server_time` / `sampling_enabled` / `sample_interval_sec` / `stale`（最新样本超过采样间隔 3 倍）等面板辅助字段。

采样能力复用 watch 的 `WatchSession.sample_once`：`POST /api/sample` 触发「立即采样」（需启动时传 `--agent/--probe`，否则按钮置灰且接口返回 403；与后台线程共用一把锁，并发重入返回 409）；`--sample-interval N` 时 GUI 进程内起守护线程每 N 秒采样一次，瞬时异常只警告并继续——单进程即完成采样 + 展示，不传采样参数时面板只读账本。

**已知局限**：① claude meter 的 message id 去重集是内存态、不持久化——同一次采样区间内去重正确，但跨采样区间的同消息重复写入会被计两次（长驻进程配小 `--interval` 可减轻）；② 外部渠道噪声——`clean` 只是间隔充分性假设，不是消耗归因，网页/手机端的消耗仍会混入 `usage=0` 但池上涨的对；③ 采样稀疏于 1% 刻度时，只有 Δ>0 的对携带 tokens，比率会被低估——保持采样间隔足够密是前提。

## 任务与执行边界

`task.toml` 至少声明 `id`、`tier`、`prompt`、`verify`、`timeout_sec` 和 `max_tokens`。runner 不修改仓库任务目录，只把 `fixtures/` 复制到临时 cwd；agent 可以在其中读写文件。agent 退出后，runner 在相同 cwd 执行 verify（脚本本体留在仓库任务目录，不复制进工作区）。wall-clock 超时或 token 熔断都会使任务失败，但仍保留已采集的 usage。

被测 agent 命令来自 `aisubench.toml` 的 `[agents.<name>].command`，支持列表或字符串模板。命令不应把密钥写入配置；私有参数放在被 git 忽略的 `aisubench.local.toml` 或环境变量中。所有内置 mock 数据均为合成数据，框架单测不会联网。

## 适配器最小接口

meter 实现 `start()` 和 `collect() -> Usage`；`start()` 记录会话日志的文件偏移，`collect()` 只消费运行期间追加的 JSONL。quota probe 实现 `snapshot() -> dict[str, float]` 并声明 `continuous`，键是独立额度窗口名称，值是已用百分比。runner、calibrate 和 report 只依赖这些标准化结构，不依赖某一家订阅的字段名。
