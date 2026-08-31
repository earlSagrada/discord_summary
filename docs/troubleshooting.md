> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 排查与运维

> 定时任务分别在什么时候跑什么，见 [计划任务总览](./scheduled-tasks.md)。

## 先看两个日志

推送靠两条腿：**① 油猴脚本每 15 分钟导出文件**、**② 计划任务每 15 分钟消费**。任何一条停了都会出现“一段时间没有新推送”。先看两个日志：

```powershell
# cycle 每轮都会记：窗口多少条消息、推了还是跳过、为什么、有没有报错
Get-Content data\cycle.log -Tail 30
# watcher 记：inbox 里的导出有没有被处理
Get-Content data\inbox\watch.log -Tail 30
```

任务本身有没有按时跑、上次结果是什么：

```powershell
Get-ScheduledTask -TaskName Discord* | ForEach-Object {
  $i = Get-ScheduledTaskInfo -TaskName $_.TaskName
  [PSCustomObject]@{ 任务=$_.TaskName; 状态=$_.State
                     下次=$i.NextRunTime; 上次=$i.LastRunTime; 结果=$i.LastTaskResult }
} | Format-Table -AutoSize
```

`结果` 是 `0` 表示正常；`267011`（0x41303）表示**从未跑过**，刚注册的任务是这个值，不是故障。

## 日志结论对照表

| 日志 | 含义 | 处理 |
|---|---|---|
| `结果：跳过（窗口内无新消息）` + `最近消息…分钟前` 很大 | **导出停摆**：油猴脚本没在产出文件 | 见下方「导出停摆」 |
| `结果：已推送 N 条` | 正常 | —— |
| `结果：本轮失败 AuthenticationError` | **ANTHROPIC_API_KEY 失效/过期** | 去 console.anthropic.com 重建 key，写回 `.env` |
| `结果：本轮失败 …` + traceback | cycle 报错（多为 API/网络/Webhook） | 看 traceback；检查 `.env`、代理 |
| watch.log 有大段时间没有任何行 | 那段时间 inbox 没进新文件 = 导出停摆 | 见下方 |

> **失败会主动告警**：任何一轮抛异常（如 API key 失效），会往 Discord 发一条 `🛑 The pulse job failed.` 告警。同类错误只发一次；成功推送后自动复位。没有这个机制的话，这类错误会**静默**失败几十轮都没人发现。

## 常见故障

### 导出停摆

最常见的两个原因都在浏览器/系统侧，不是脚本 bug：

1. **Chrome 把后台的 Discord 标签页“丢弃/冻结”了**。长时间不看那个标签，浏览器会冻结甚至卸载它，定时器就停了。
   - 缓解：把 Discord 标签**固定(pin)**、尽量别最小化太久；或在 `chrome://discards` 里把 discord.com 设为不可丢弃；或用一个单独的窗口只放 Discord。
   - 脚本 v0.4 起，右下角面板会显示「上次导出 时间（N 分钟前）」，**超过 2 个周期没动静会标红 ⚠停摆?**，控制台也会告警。
2. **电脑睡眠**。睡眠时脚本和计划任务都会暂停。
   - 任务已设 `WakeToRun`（到点唤醒）+ 电池也运行；但**浏览器里的油猴定时器无法唤醒系统**，所以睡眠期间的消息仍会漏。
   - 盯盘时段建议把电源计划设为不睡眠。

> 长缺口（夜里几小时）通常就是关机/睡眠，属正常。要留意的是**盘中**的 1–3 小时缺口，一般就是上面第 1 条。

cycle 也会做停摆检测：最新消息超过 90 分钟没更新时，会往 Discord 发一次 `⚠️ Exporter looks stalled.`；本次停摆只发一次，恢复后自动清标志。

### API key 失效 / API 连不上

- `AuthenticationError` 通常是 `ANTHROPIC_API_KEY` 失效或过期：重建 key，写回 `.env`。
- 连不上 API 时检查 `HTTPS_PROXY`。`requests` 和 `anthropic` SDK 都读这个环境变量。

### Webhook 问题

`.env` 里的 `DISCORD_WEBHOOK_URL` 写错、被删，或者网络异常，都会让 cycle 推送失败。可以单独测 Webhook：

```powershell
# 单独测 Webhook 通不通
.\.venv\Scripts\python.exe src\discord_post.py "test from cycle"
```

### 图片 403 / 404

签名过期了。重新导出一次，或者用 JSON 文件里保留的消息 ID 回到 Discord 手动看。养成导出后立刻跑 `enrich_images.py` 的习惯。

### 消息缺漏

滚动太快。降低速度重滚一遍，脚本按 message ID 去重，重复采集不会产生重复条目。也可以不点“清空”，分几次滚完再一起导出。

### 导出的作者名全是 `(unknown)`，或者一条都抓不到

Discord 的 CSS class 名是 hash 过的，改版会失效。在 F12 控制台运行：

```js
__digest.diagnose()
```

会打一张表，哪一项是 0 就是哪个选择器坏了。修复方法：在 Discord 页面里右键一条消息 → 检查，看看新的属性名，改脚本里对应的 `[class*="..."]`。用 `id^=` 开头的那几个（`message-content-`、`chat-messages-`）比较稳定，基本不会变。

### 日报看起来没写完（末尾半句话/半张表）

先用 `--debug` 重跑，查看 `.digest.debug.jsonl` 里是否出现 `"stop_reason": "max_tokens"`。脚本会自动续写；如果仍连续触发上限，请提高 `--max-tokens`，或减少输入规模后再生成。

## 磁盘清理

清理脚本默认只报告，不会删文件：

```powershell
.\.venv\Scripts\python.exe src\cleanup.py
# 真正执行：
.\.venv\Scripts\python.exe src\cleanup.py --apply
```

| 目标 | 保留 | 说明 |
|---|---:|---|
| `data\inbox\processed\` | 15 天 | 原始导出已合并进 `chats_by_date`，短期留存便于排查 |
| `data\market_cache\` | 3 天 | 行情/期权/事件按天缓存，只有当天会被读 |
| `data\cache\images\` | 7 天 | 图片原图；转写结果已进缓存 |

这些永不自动删除：

- `data\cache\transcripts.json`、`data\cache\url_index.json`：图片转写缓存，删了会重新花钱调 AI。
- `data\chats_by_date\`：核心语料，`pulse --last` / `review` 依赖。
- `data\signals.db`：信号台账，永久留存。
- `data\inbox\processed\_processed.json`：watcher 去重状态，删了会重处理历史文件。

日志只按大小轮转：`data\cycle.log`、`data\inbox\watch.log` 超过 5 MB 时，旧文件覆盖到 `.1`，当前日志只保留最后 2000 行。

注意：`data\inbox` 可能同时是 Chrome 下载目录，容易混入无关大文件。清理脚本**不会**删除 inbox 根目录文件，只会在报告里提醒超过 20 MB 的文件，请人工确认后处理。

## 成本估算

一天几百条消息的精简文本大约几千到一万 token。用 `claude-sonnet-5` 做日报，单次成本很低。图片转写默认走 `claude-haiku-4-5-20251001`，更便宜，且有缓存不会重复计费。想要更好的图表理解可以：

```bash
python src/enrich_images.py in.txt --model claude-sonnet-5
```
