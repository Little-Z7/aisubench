# AISUBench macOS 状态栏壳

纯原生 macOS 菜单栏监控应用（rumps）：在状态栏常驻显示最紧急额度池的
`池名 已用%`，下拉菜单展示与 `python3 -m aisubench status` 同口径的
各池数字（已用≈tokens、Q 区间、速率、ETA）与样本新鲜度。

**硬约束**：无浏览器跳转、无 WebView、无本地 HTTP 服务。取数靠进程内
`aisubench.status.collect_status`，采样靠进程内 daemon 线程复用
`aisubench.watch` 的 `WatchSession`，不开任何端口。

## 运行

```bash
# 一次性依赖（仅壳需要；aisubench 核心包仍是纯标准库）
pip install -r shells/macos/requirements.txt

# 采样 + 展示单进程：每 300s 采样一次、每 30s 刷新菜单
python3 -m shells.macos --agent mock --probe mock

# 只读账本（「立即采样」置灰），配合 cron 跑 watch --once 的场景
python3 -m shells.macos --no-sample
```

## 参数

- `--agent NAME --probe mock|arkcli|manual`：启用进程内采样与菜单「立即采样」；
  两者需同时提供。meter/log_path 等仍取 `aisubench.toml` 的 `[agents.*]`。
- `--interval SEC`：采样间隔，默认 300。
- `--refresh SEC`：菜单刷新间隔，默认 30。
- `--no-sample`：不启用采样，只读账本。
- `--ledger PATH`：账本路径，默认取 `[watch].ledger`（`state/ledger.jsonl`）。

菜单结构：头部信息行（置灰）→ 每池标题行 + 详情行（`!`/`!!` 前缀标记
warn/critical）→「立即采样」「重新读取」「退出」。最后采样距今超过
30 分钟时头部追加「数据陈旧」。
