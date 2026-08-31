> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 周期复盘

周期复盘把最近 N 天聊天记录和信号胜率交给 Claude sonnet，生成一份中文交易复盘。

## 做什么

`src\review.py` 的流程：

1. 收集最近 N 天 `data\chats_by_date\<日期>\<频道>\merged.enriched.txt`。
2. 调 `backtest.report_text()` 和 `backtest.suggestions_text()` 取胜率统计与调参建议。
3. 用 `prompts\weekly_review.md` 生成中文 Markdown 复盘。
4. 完整报告写入 `trade_notes\reviews\<日期>-review.md`。
5. 抽取 `TL;DR` 段落推送到 Discord。
6. 抽取 `VIP 名单建议` 写入 `prompts\vip_suggestions.md`。

## 报告结构

| 区块 | 内容 |
|---|---|
| TL;DR（推送摘要） | 3–5 条最重要结论，会发到 Discord |
| 有价值的发言人 | 谁有逻辑、有证据，哪些只是噪音 |
| 成功方法复盘 | 哪些方法赚钱，哪些心态或做法亏钱 |
| 信号引擎校准 | 结合胜率统计，给调参建议 |
| VIP 名单建议 | 建议新增或移除的重点发言人 |

方法论由 prompt 固定强调：

- 右侧确认 > 左侧预测。
- 真实业务催化 > 技术破位 > 资金结构 > 宏观口水。
- 纪律 > 观点。
- 做 priced-in / sell the news 检查。
- 只喊方向、无证据、无晒单的发言当噪音。

## VIP 建议不会自动覆盖

`review.py` 只写：

```text
prompts\vip_suggestions.md
```

它不会自动改：

```text
prompts\vip_speakers.txt
```

请人工确认建议后，再手动更新重点发言人名单。

## 命令

```bash
python src\review.py --days 7
python src\review.py --days 7 --no-post
python src\review.py --no-api
```

| 参数 | 作用 |
|---|---|
| `--days N` | 回看最近 N 天，默认 7 |
| `--no-post` | 只写完整报告，不推送 Discord |
| `--no-api` | 不调 Claude，只打印喂给模型的 prompt 预览 |
| `--model` | 覆盖默认 sonnet 模型 |

## 计划任务

维护任务脚本会注册 4 个任务，完整说明见 [计划任务总览](scheduled-tasks.md)：

| 任务 | 默认时间 | 作用 |
|---|---|---|
| `DiscordBacktestDaily` | 每天 23:30 | 回填 outcomes |
| `DiscordSelfReviewDaily` | 每天 23:45 | 每日自我复盘并更新 playbook |
| `DiscordWeeklyReview` | 每周日 20:00 | 生成周期复盘并推送 TL;DR |
| `DiscordCleanupWeekly` | 每周日 22:00 | 磁盘清理与日志轮转 |

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1
```
