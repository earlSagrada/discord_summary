# Discord 交易群 · 聊天整理与盯盘辅助

把 Discord 交易群的聊天导出成文本（保留引用关系和图片内容），交给 Claude 做话题聚类，
再叠加行情/期权/宏观数据，产出**每 15 分钟一条的"脉搏简报 + 信号卡"**推到你自己的频道。

```
Discord 网页
   │  userscript（浏览器里跑，读渲染后的 DOM）
   ▼
data/inbox/*.json/.txt          导出文件
   │  watch_inbox.py → enrich_images.py（图片转写）
   ▼
chats_by_date/<日>/<频道>/merged.enriched.txt
   │
   ├─ digest.py   → 日报
   └─ cycle.py    → 脉搏简报(Claude) + 信号卡(本地规则) → Discord Webhook
                      ↑ 行情/关键位 · 期权 · 宏观事件 · 信号台账
```

> ⚠️ 这是决策**辅助**，不是荐股。信号规则是 v0 粗版，务必自己复核；仓位/风险自负。

---

## 📚 文档

完整说明都在 **[docs/](docs/README.md)**，按需查阅：

| 想了解 | 看这里 |
|---|---|
| 怎么装、每天怎么用 | [安装与日常操作](docs/setup.md) |
| **6 个定时任务分别在干什么** | [计划任务总览](docs/scheduled-tasks.md) |
| 整体数据流、各模块职责 | [架构与目录结构](docs/architecture.md) |
| 每 15 分钟的简报 / 回看历史时段 | [脉搏简报](docs/pulse.md) |
| **消息老是重复？只讲变化** | [增量脉搏](docs/delta-pulse.md) |
| **关键位怎么算、财报怎么分析** | [信号打分](docs/signals.md) |
| **信号是不是已被市场 price in** | [priced-in 判定](docs/priced-in.md) |
| 期权 IV / call·put wall 怎么用 | [期权数据确认](docs/options.md) |
| 今天是不是 FOMC/CPI 日 | [宏观事件日历](docs/macro-events.md) |
| **系统怎么自我学习变强** | [每日自我复盘](docs/self-review.md) |
| **系统怎么自己调参数、怎么回滚** | [自我优化](docs/optimization.md) |
| **信号到底准不准** | [回测统计](docs/backtest.md) |
| 每周"谁值得听、什么方法赚钱" | [周期复盘](docs/review.md) |
| 推送停了 / 报错了 / 成本 | [排查与运维](docs/troubleshooting.md) |

方法论依据见 [trade_notes/交易信号复盘与方法总结.md](trade_notes/交易信号复盘与方法总结.md)。

---

## 快速开始

```powershell
# 1) 环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 配置：复制 .env.example 为 .env，填 ANTHROPIC_API_KEY 和 DISCORD_WEBHOOK_URL
#    （可选：FINNHUB / FMP / FRED / POLYGON）

# 3) 装浏览器脚本：Tampermonkey 新建脚本，粘贴 userscript/discord-digest-exporter.user.js
#    打开 Discord 频道，开启面板上的「定时导出」

# 4) 注册计划任务
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1                 # 每15min 推送
powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1    # 每日回测+自我复盘、每周复盘+优化+清理
```

详见 [安装与日常操作](docs/setup.md)。

---

## 常用命令

```powershell
# 跑一轮（入库→简报→信号→推送）
.\.venv\Scripts\python.exe src\cycle.py --once

# 自测：不推送、不入库，打印将要发送的内容
.\.venv\Scripts\python.exe src\cycle.py --once --dry-run --no-watch --anchor last --date 20260827

# 回看历史时段的脉搏（可加 --signals 附带期权信号卡）
.\.venv\Scripts\python.exe src\pulse.py --last 3d
.\.venv\Scripts\python.exe src\pulse.py --from 20260813 --to 20260814 --signals

# 给指定标的打分
.\.venv\Scripts\python.exe src\signals.py --watchlist SOXL,NVDA,QQQ --no-save

# 期权指标 / 今日宏观事件
.\.venv\Scripts\python.exe src\options.py NVDA
.\.venv\Scripts\python.exe src\events.py

# 回测与复盘
.\.venv\Scripts\python.exe src\backtest.py --backfill --report
.\.venv\Scripts\python.exe src\selfreview.py --yesterday    # 每日自我复盘（会更新 playbook）
.\.venv\Scripts\python.exe src\review.py --days 7           # 每周复盘
.\.venv\Scripts\python.exe src\optimize.py --show          # 看当前生效的可调参数
.\.venv\Scripts\python.exe src\optimize.py                 # 自我优化（opus，会改参数/方法库）
.\.venv\Scripts\python.exe src\vips.py                     # 看重点发言人名单 + 待确认建议

# 解析 Frank 的 Substack 复盘 PDF（放进 data/substack_post/ 会自动触发系统校准）
.\.venv\Scripts\python.exe src\substack.py
.\.venv\Scripts\python.exe src\substack_pipeline.py --force   # 手动触发校准流水线
```

---

## 目录速览

```
src/          Python 管线（见 docs/architecture.md）
docs/         使用文档
prompts/      喂给 AI 的 prompt（改措辞不用动代码）
userscript/   浏览器导出脚本
scripts/      Windows 计划任务注册脚本
trade_notes/  方法论、设计文档、实施记录、周期复盘产出
data/         运行时数据（聊天记录、缓存、signals.db 台账、日志）
data/substack_post/   Frank 的 Substack 复盘 PDF（自我复盘的方法论标杆）
event_calendar.json   用户维护的 FOMC 等宏观事件日期表（每年核对）
.env          API key + Webhook（已 gitignore）
```

---

## 可以自己改的地方

- **`prompts/*.md`**——所有 AI 措辞（脉搏简报、日报、周期复盘）都在这，改 prompt 不用动代码。
- **`prompts/playbook.md`**——分析方法库，自我复盘自动维护，也可以手工增删；改完下一轮简报立刻生效。
- **`prompts/vip_speakers.txt`**——重点发言人名单，一行一个。
- **`src/config.py`**——新鲜度/期权/回测的各种阈值集中在顶部。
- **`src/tickers.py`**——标的宇宙与黑话别名词典。
- **`src/enrich_images.py` 的 `TRANSCRIBE_PROMPT`**——图片转写的详略程度。
