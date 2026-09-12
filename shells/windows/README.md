# AISUBench Windows 悬浮球壳

纯原生 Windows 桌面悬浮球监控（PySide6/Qt）：一个无边框、置顶、半透明的
小圆球常驻桌面，球面显示最紧急额度池的已用%（如 `86%`）；左键点击展开
详情面板——每池一行彩色进度条（<70% 绿 / <90% 黄 / ≥90% 红）加与
`python3 -m aisubench status` 同口径的数字（已用≈tokens、Q 区间、速率、
ETA），标题按 ETA 告警级别着色（warn 黄 / critical 红）；再点球或点面板
外任意处收起。左键拖拽可移动球，位置记入 `state/ball_position.json`，
下次启动自动恢复。右键菜单：「立即采样」「退出」。

**硬约束**：无浏览器、无 WebView、无本地 HTTP 服务。取数靠进程内
`aisubench.status.collect_status`，采样靠进程内 daemon 线程
（`shells.shared.sampler`，与 macOS 壳共用同一实现，复用
`aisubench.watch` 的 `WatchSession`），不开任何端口。

## 安装与运行（Windows）

```powershell
# 一次性依赖（仅壳需要；aisubench 核心包仍是纯标准库）
py -m pip install -r shells\windows\requirements.txt

# 采样 + 展示单进程：每 300s 采样一次、每 30s 刷新球面
py -m shells.windows --agent mock --probe mock

# 只读账本（右键「立即采样」置灰），配合计划任务跑 watch --once 的场景
py -m shells.windows --no-sample
```

也可用 `python` / `python3` 替代 `py`，按本机 Python 启动器习惯。

## 参数

- `--agent NAME --probe mock|arkcli|manual`：启用进程内采样与右键
  「立即采样」；两者需同时提供。meter/log_path 等仍取 `aisubench.toml`
  的 `[agents.*]`。
- `--interval SEC`：采样间隔，默认 300。
- `--refresh SEC`：球面/面板刷新间隔，默认 30。
- `--no-sample`：不启用采样，只读账本。
- `--ledger PATH`：账本路径，默认取 `[watch].ledger`（`state/ledger.jsonl`）。
- `--position-file PATH`：球位置记忆文件，默认 `state/ball_position.json`。

面板头部为样本数/跨度与最后采样时间（超过 30 分钟追加「数据陈旧」），
与 macOS 状态栏壳共用 `shells/shared/viewmodel.py` 的全部文案。
