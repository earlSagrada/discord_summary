> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 架构与目录结构

## 整体数据流

```text
Discord 网页
   │  discord-digest-exporter.user.js（浏览器里跑，读渲染后的 DOM）
   ▼
discord-YYYYMMDDHHMM.txt          精简文本，图片是 [IMG#1] 占位符
discord-YYYYMMDDHHMM.json         完整结构化数据（备份用）
discord-YYYYMMDDHHMM-images.txt   图片 URL 清单
   │  enrich_images.py（下载 + 转写 + 缓存）
   ▼
discord-YYYYMMDDHHMM.enriched.txt 图片已替换成文字
   ├─ digest.py  → discord-YYYYMMDDHHMM.digest.md（日报）
   └─ cycle.py / pulse.py / signals.py
        1. watch_inbox：去重合并 + enrich，按频道落到 chats_by_date/<日>/<频道>/
        2. 切最近 N 分钟窗口（两个频道合并）
        3. Claude(haiku) 生成中文脉搏简报（说人话、可扫读）
        4. 从当天讨论抽重点标的，重新打分，得到最新信号卡（中文）
        5. 简报 + 信号卡拼成一条，POST 到 DISCORD_WEBHOOK_URL 指向的频道
```

## `src/` 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 集中路径 + `.env` 加载（所有脚本 import 它） |
| `prompts.py` | 读取 `prompts/` 里的 prompt 文件（一个用途一份） |
| `enrich_images.py` / `digest.py` / `watch_inbox.py` | 聊天入库与日报 |
| `pulse.py` | 脉搏简报：切最近 N 分钟窗口 → Claude → 中文简报；支持 `--last` / `--from` / `--to` 历史区间；带跨轮记忆与 playbook 注入 |
| `delta.py` | 变化检测：对比今天已有记录，算出灯色/新票/热度/价格进展（不调 AI，数字只能算不能猜） |
| `discord_post.py` | 把消息 POST 到 Discord 频道 Webhook（自动分片） |
| `signal_format.py` | 信号卡 → 中文推送文本（静态模板，不调 AI） |
| `cycle.py` | 编排器：入库→脉搏简报→信号→推送（挂计划任务每 15min） |
| `tickers.py` | 标的宇宙 + 黑话/别名词典（ETP/ETF 为重点） |
| `extract.py` | 从聊天文本抽 ticker（正则 + 词典）；`last_mention_times()` 供“聊天已不热”判定 |
| `market.py` | yfinance 行情 / Finnhub 财报（带每日缓存） |
| `options.py` | yfinance 期权链 → IV / put·call wall / P·C 比 / 异常量（B 档确认，只提示不给方向） |
| `events.py` | 宏观事件日历：FRED 发布日期 + 静态 FOMC 表 + Finnhub/FMP → 自动 `--event-today` |
| `levels.py` | 关键位计算（前高低 / 均线 / VWAP / 量比）+ `breakout_age`（突破几天前发生） |
| `store.py` | SQLite 落库（`signals.db`）+ outcomes 回填/回测查询 + `pulse_rounds` 推送记忆 |
| `signals.py` | 信号打分（`analyze()` 可被 cycle 复用）→ 信号卡 + 存库；含新鲜度/priced-in 降级 |
| `backtest.py` | 回填 outcomes（T+1/3/5 走势）+ 胜率统计 + 调参建议（模块 6 数据闭环） |
| `substack.py` | 解析 Frank 的 Substack 复盘 PDF → 纯文本（带文本质量校验，防止字体缺映射导致数字全丢） |
| `selfreview.py` | **每日自我复盘**：学群里和 Frank 的推理方法、找我们自己的短板 → 写回 `prompts/playbook.md` |
| `optimize.py` | **系统自我优化**（opus）：按胜率调阈值、增删方法库，写含"否决替代方案"的决策记录 |
| `vips.py` | 重点发言人名单管理：解析复盘建议、连续 2 期才自动落地、可 `--undo` |
| `substack_pipeline.py` | 新 Substack PDF 落地 → 自动跑「复盘 + 优化 + VIP 确认」 |
| `review.py` | 周期复盘：有价值发言人 + 成功方法 + 信号校准 → 报告文件 + Discord 摘要 |
| `cleanup.py` | 分级磁盘清理 + 日志轮转（默认试运行） |

## 其他目录与文件

| 路径 | 说明 |
|---|---|
| `prompts/` | 喂给 AI 的 prompt（一个用途一份，改措辞不用动代码） |
| `prompts/pulse_summary.md` | 脉搏简报（中文，含催化分级 + priced-in 检查 + 只讲增量 + 今日主线） |
| `prompts/playbook.md` | **分析方法库**：自我复盘写入，pulse 每轮取前 8 条注入 prompt（自我改进闭环的载体） |
| `prompts/daily_review.md` | 每日自我复盘（中文） |
| `prompts/optimize.md` | 系统自我优化（中文，强制写"否决的替代方案"） |
| `prompts/weekly_review.md` | 周期复盘（中文） |
| `prompts/vip_speakers.txt` | 重点发言人名单（`review.py` / `selfreview.py` 会往 `vip_suggestions.md` 提增删建议） |
| `prompts/digest_system.md` / `prompts/digest_user.md` / `prompts/digest_merge.md` | 日报 prompt |
| `userscript/` | `discord-digest-exporter.user.js`（浏览器里跑，支持多频道轮流采集） |
| `data/` | 运行时数据（`inbox/`、`cache/`、`market_cache/`、`signals.db` 不入库） |
| `data/inbox/` | 浏览器下载落地处，watcher 处理后归档到 `processed/` |
| `data/substack_post/` | Frank 的 Substack 复盘 PDF + 解析出的 `.txt`（新文件落地会自动触发系统校准） |
| `data/tunables.json` | AI 调过的参数覆盖层（删掉即回到出厂默认） |
| `data/cache/` | 图片哈希缓存 |
| `data/chats_by_date/` | 按日 + 按频道整理：`<日>/<频道>/merged.enriched.txt` |
| `data/chat_frank/` | Frank 频道导出 |
| `data/market_cache/` | yfinance 行情当日缓存 + `events_<日>.json` 事件缓存（可随时删） |
| `data/signals.db` | 信号台账（SQLite，永久留存） |
| `event_calendar.json` | 用户维护的宏观事件日期表（FOMC 等，每年更新；`events.py` 读取） |
| `scripts/` | `register_task.ps1`（15min 脉搏推送）+ `register_maintenance_tasks.ps1`（每日回测 + 每周复盘） |
| `trade_notes/` | 分析与工具设计文档（含 `MVP-v0-实施记录.md`，讲自动化+数据源）；`reviews/` 存周期复盘 |
| `.env` | 你的 API key + Webhook（已 gitignore；从 `.env.example` 复制） |

> 每 15 分钟自动导出 + 本地自动入库的完整流程见 [trade_notes/MVP-v0-实施记录.md](../trade_notes/MVP-v0-实施记录.md)。
