# 评测指标口径

## Usage 口径

每个运行结果的 `usage` 记录 `input`、`output`、`cached`、`requests` 和时间字段，口径如下：

- `input`：**非缓存**输入 token；供应商的 cache_creation（缓存写入）计入 `input`。
- `cached`：缓存命中（cache read）单独记录，不并入 `input`。
- `total`：`input + cached + output`。

若日志只有供应商自带的 total 字段，适配器可以将其作为回退值，但应在报告备注中说明转换。

## 速度

- **TPS_gen**：`output_tokens / (last_token_ts - first_token_ts)`。缺少任一时间戳、时间差不大于 0，或没有 output token 时记为 0；不能用 wall time 代替生成时间。两个时间戳量纲不一致（一个是 epoch 秒、另一个是 ISO 字符串，无法换算成同一时间轴）时记为 0。
- **TPS_wall**：`total_tokens / wall_time`。wall time 用**结果级**的 `result.json` 顶层 `wall_time`（runner 在 agent 进程上实测的耗时），包含工具调用、等待和重试，但不包含退出后的 verify 时间。

## 任务消耗与通过

- **tokens/task（全部）**：`Σ全部任务 total_tokens / 全部任务数`。失败、超时和触发 `max_tokens` 熔断的任务都计入。
- **tokens/task（仅通过）**：`Σ通过任务 total_tokens / 通过任务数`。没有通过任务时记为**不可用**，不得显示 0。
- **通过率**：`通过任务数 / 全部任务数`；空结果集时记为不可用。任务是否收敛只看 verify 命令退出码是否为 0。agent 自报完成不会改变收敛判定，若两者不一致会记录为假收敛。

## 订阅折算

设某订阅窗口价格为 `P` 元，标定得到该窗口 100% 额度的 token 当量为 `Q`：

- **元/task**：`tokens/task（全部） × P / Q`。
- **元/有效任务**：`Σ全部任务 total_tokens / 通过任务数 × P / Q`。失败消耗仍在分子中；通过数为 0 时报告为不可用而不是 0。

`P / Q` 是本次订阅额度标定的有效折算单价，不是供应商公开 API 的发票单价。不同窗口应分别计算，不能把 5 小时、周、月额度混为一个 Q。

`aisubench report` 的 `--price` / `--quota-tokens` 缺省时：价格自动取 `aisubench.toml` 的 `[subscriptions.*]` 价格，`Q` 自动取 `reports/` 下最新一份 `calibration-*.json` 中第一个可用窗口的 100% token 当量；两者都拿不到时才省略元/task 行。

## 限额 token 当量与置信区间

对一次标定，设任务累计消耗为 `S`，额度从 `before` 变为 `after`，变化为 `Δ = after - before` 个百分点：

`R = tokens_per_quota_pct = S / Δ`（tokens/%）

窗口 100% 的 token 当量为：`Q = 100 × R`。

若百分比最小显示粒度为 `g`，则 `before`/`after` 每次读数的量化误差为 ±g/2，`Δ` 是两次读数之差，最坏误差相加为 **±g**：

- `R_low = S / (Δ + g)`；
- `R_high = S / (Δ − g)`，当 `Δ − g ≤ 0`（即 `Δ ≤ g`）时上界不可用，必须报告为不可用，不得伪造精度；
- `Q_low = 100 × R_low`、`Q_high = 100 × R_high`，同样在 `Δ ≤ g` 时上界不可用。

**可分辨判据**：只有 `Δ > g` 时才认为该窗口的 token 当量可分辨。

tokens=0 但 `Δ>0` 的窗口说明没有采集到任何用量，token 当量记为不可用，不输出伪有效的 0 值。

标定循环在只看**活跃窗口**（after 中仍存在、且参与结算的窗口）时提前停止：全部活跃窗口都满足 `Δ > g` 即可停止，或达到 `max_tasks` 预算上限。限流、降速、失败和超时不能从样本中删除，应同时在通过率、wall time 和报告备注中保留。

## 持续估计口径

`calibrate`（bench）是一次性主动实验，给出各窗口 token 当量 `Q` 的**先验**；`watch + status` 在真实日常使用中长期被动采样，对同一批窗口做**持续修正的后验**，两者共用 ±g 读数误差口径，公式见 `docs/benchmark-design.md` 的「持续监测」一节。

对持续监测的账本样本（契约见 `aisubench/ledger.py`），各池另派生三个展示量（`aisubench status`）：

- **比率估计**：`R = Σtokens / ΣΔ`，对全部 Δ>0 且未被重置打断的相邻样本对累加；`Q = 100×R`，区间 `[100×Σtokens/(ΣΔ+g), 100×Σtokens/(ΣΔ−g)]`，`ΣΔ ≤ g` 时上界不可用。`--clean-only` 时只用两端均 clean 的对，排除未观测渠道。
- **消耗速率**：`rate = Σ_{窗口内} total_tokens ÷ 窗口实际跨度（小时）`，窗口右端为该池最新样本，`--window-hours` 默认 24。
- **监测 TPS**：`tps = rate ÷ 3600`（tokens/秒，保留两位小数），即把上面的消耗速率换算成秒均，展示在 `status` 表与 GUI 面板。它是被动监测的**平均消耗**换算，与上文 bench 的 **TPS_gen / TPS_wall**（单次运行的生成速度：output tokens ÷ 首末 token 时间差、总 tokens ÷ wall time）**不同口径**，不可混比；速率不可用时 TPS 同样不可用。订阅级 TPS 取该订阅各池 TPS 的最大值（没有跨池统一窗口速率可聚合时的简化）。
- **预计耗尽**：`ETA = (100 − 当前已用%) × R ÷ rate`（小时）。当前已用% 取该池最新一次读数；任一因子缺失、非正、或池已用满时显示**不可用**，不显示 0。

与 bench 先验的关系：标定消耗可控、归因干净但样本对少（单次 Δ 粗）；持续监测样本对多、能覆盖真实混合负载和窗口重置，但依赖 clean 假设且受外部渠道噪声影响。实践建议先跑 `calibrate` 拿到 Q 的先验，再用 `watch`（如 cron `--once`）积累账本，由 `status` 观察后验区间是否收敛到先验附近——两者显著背离时优先怀疑外部消耗污染或采样过疏，而非订阅改版。
