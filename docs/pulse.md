> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 脉搏简报（Pulse）

Pulse 是每 15 分钟推送一次的群聊脉搏：先读 Discord 导出的聊天，再让 Claude haiku 写中文简报，最后附上最新信号卡推到你的 Discord 频道。

## 自动推送链路

```text
油猴脚本（Discord 标签页开着）
  └─ 每 15min 导出 JSON 到 data\inbox\
      └─ cycle.py --once（Windows 计划任务触发）
          1. watch_inbox 去重、合并、enrich
          2. 切最近 N 分钟窗口，合并当天各频道消息
          3. Claude haiku 生成中文简报（说人话、不超过 500 字）
          4. 从当天讨论抽标的，生成中文信号卡
          5. 简报 + 信号卡 POST 到 DISCORD_WEBHOOK_URL
```

省钱护栏：

- 窗口内没有新消息：跳过 AI、跳过推送。
- 消息很少：仍可推送，但头部会提示低活跃。
- `--always` 可强制窗口空也发，主要用于调试。

追补机制：

- `cycle.py` 会记录上次成功推送时间到 `data\cycle_state.json`。
- 如果计划任务停了一段时间，下一轮会把窗口放大到缺口长度。
- 追补上限是 12 小时，避免一次推送过长。
- 追补推送头部会写 `Catch-up`，说明覆盖的是缺口区间。

## 一次性准备

1. 建 Webhook：自己的 Discord 频道 → 频道设置 → 整合(Integrations) → Webhook → 新 Webhook → 复制 URL。
2. 填 `.env`：

```text
DISCORD_WEBHOOK_URL=...
ANTHROPIC_API_KEY=...
```

3. 注册计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# 卸载： ... register_task.ps1 -Remove
```

任务名是 `DiscordDigestCycle`。可手动跑一次：

```powershell
Start-ScheduledTask -TaskName DiscordDigestCycle
```

前提：Discord 标签页保持打开，油猴脚本打开“定时导出”，负责把文件写入 `data\inbox\`。

## 手动 / 自测

```powershell
# 生产：跑一轮（入库→简报→信号→推送）
.\.venv\Scripts\python.exe src\cycle.py --once

# 自测：不推送、不入库、锚到最后一条消息，只打印将要发送的内容
.\.venv\Scripts\python.exe src\cycle.py --once --dry-run --no-watch --anchor last --date 20260803

# 只看脉搏简报喂进去的窗口（不调 AI）
.\.venv\Scripts\python.exe src\pulse.py data\chats_by_date\20260803\frank\merged.enriched.txt --minutes 45 --anchor last --no-api

# 单独测 Webhook 通不通
.\.venv\Scripts\python.exe src\discord_post.py "test from cycle"
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--minutes N` | 窗口大小，默认 45 分钟 |
| `--limit N` | 最多给多少个标的打信号卡 |
| `--event-today` | 手动强制标为大宏观日，压绿灯 |
| `--always` | 窗口空也照发 |
| `--no-save` | 不写 `data\signals.db` |
| `--no-watch` | 跳过 inbox 处理，用已有 enriched 文件 |
| `--anchor last` | 离线测试时锚到最后一条消息 |
| `--date YYYYMMDD` | 强制处理某天，测试用 |

## 历史区间 pulse

自动推送只看“最近 N 分钟”。如果要回看任意历史区间，用 `pulse.py` 区间模式。触发方式是命令行，因为系统只有出站 Webhook，没有能读 Discord 消息的入站 bot。

```powershell
# 相对：最近 3 天 / 6 小时 / 90 分钟（90m / 6h / 3d）
.\.venv\Scripts\python.exe src\pulse.py --last 3d

# 绝对 UTC 区间（YYYYMMDD 或 YYYYMMDDHHMM；--to 省略=到现在）
.\.venv\Scripts\python.exe src\pulse.py --from 20260813 --to 20260814

# 只看喂进去的窗口、不调 AI
.\.venv\Scripts\python.exe src\pulse.py --last 3d --no-api

# 简报后附上"这段时间点到的票"的带期权信号卡（IV / 支撑阻力 / 异常量 / capped）
.\.venv\Scripts\python.exe src\pulse.py --last 3d --signals

# 把历史简报推送到 Discord
.\.venv\Scripts\python.exe src\pulse.py --from 20260813 --to 20260814 --post
```

历史参数：

| 参数 | 作用 |
|---|---|
| `--last 90m/6h/3d` | 以现在为终点，回看最近一段时间 |
| `--from YYYYMMDD[HHMM]` | UTC 起点 |
| `--to YYYYMMDD[HHMM]` | UTC 终点；省略则到现在 |
| `--no-api` | 只打印切出来的聊天窗口，不调 Claude |
| `--post` | 把历史简报推送到 Discord |
| `--signals` | 附带期权信号卡；用当前行情数据，不是历史行情 |

## 简报输出结构

Pulse prompt 在 `prompts\pulse_summary.md`。输出是**中文、说人话、不超过 500 字**，固定结构：

| 区块 | 内容 |
|---|---|
| `🔥 现在在聊什么` | 3–5 条当前话题：哪只票、看多看空、谁说的、**理由是什么**，末尾标催化等级 |
| `🧊 是不是已经被消化了` | 0–2 条，判断消息是否已反映在价格里（sell the news / 需多日确认）|
| `⭐ 重点发言人` | 重点人物说了什么、给了什么理由 |
| `📋 提到的标的` | 一行列完：`NVDA(多·A) SOXL(空·B)` |
| `⚠️ 存疑` | 0–3 条没证据的说法；只喊方向不晒单的标"未证实" |

### 排版是强制的，不靠模型自觉

`pulse.tighten()` 会对 AI 输出做后处理，保证格式稳定：

- `# 标题` / `## 标题` → `**标题**`（Discord 里 `#` 会渲染成超大字号，很占屏）
- **删掉所有空行**（标题前、条目前都不留空行，推送更紧凑）
- 去掉行尾空白

所以即使模型偶尔不听话用了 `#` 或多打了空行，推送出来的格式也是一致的。

催化分级：

| 等级 | 含义 |
|---|---|
| `A` | 真实业务催化：财报、订单、合同等 |
| `B` | 技术位或资金结构：breakout、VWAP、gamma、期权大单等 |
| `C` | 只有宏观口水或无证据观点 |
