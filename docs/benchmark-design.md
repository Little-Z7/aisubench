# AISUBench 基准设计

## 架构

AISUBench 由任务、runner、meter、quota probe、指标和报告组成。任务目录包含 `task.toml`、`fixtures/` 和验收脚本。runner 将 fixtures 复制到临时工作目录，以该目录为当前目录启动被测 agent，agent 退出或超时后执行验收脚本。验收脚本退出码为 0 即表示任务收敛；agent 自报完成但验收失败会记录为假收敛。

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

## 任务与执行边界

`task.toml` 至少声明 `id`、`tier`、`prompt`、`verify`、`timeout_sec` 和 `max_tokens`。runner 不修改仓库任务目录，只把 `fixtures/` 复制到临时 cwd；agent 可以在其中读写文件。agent 退出后，runner 在相同 cwd 执行 verify（脚本本体留在仓库任务目录，不复制进工作区）。wall-clock 超时或 token 熔断都会使任务失败，但仍保留已采集的 usage。

被测 agent 命令来自 `aisubench.toml` 的 `[agents.<name>].command`，支持列表或字符串模板。命令不应把密钥写入配置；私有参数放在被 git 忽略的 `aisubench.local.toml` 或环境变量中。所有内置 mock 数据均为合成数据，框架单测不会联网。

## 适配器最小接口

meter 实现 `start()` 和 `collect() -> Usage`；`start()` 记录会话日志的文件偏移，`collect()` 只消费运行期间追加的 JSONL。quota probe 实现 `snapshot() -> dict[str, float]` 并声明 `continuous`，键是独立额度窗口名称，值是已用百分比。runner、calibrate 和 report 只依赖这些标准化结构，不依赖某一家订阅的字段名。
