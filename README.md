# Discord 交易群聊天记录整理流程

把 Discord 频道的当日聊天导出成纯文本（保留引用关系和图片内容），
再交给 Claude 做话题聚类和黑话注释。

```
Discord 网页
   │  discord-digest-exporter.user.js（浏览器里跑，读渲染后的 DOM）
   ▼
discord-YYYYMMDDHHMM.txt          精简文本，图片是 [IMG#1] 占位符
discord-YYYYMMDDHHMM.json         完整结构化数据（备份用）
discord-YYYYMMDDHHMM-images.txt   图片 URL 清单
   │  enrich_images.py（下载 + 转写 + 缓存）
   ▼
discord-YYYYMMDDHHMM.enriched.txt 图片已替换成文字
   │  digest.py
   ▼
discord-YYYYMMDDHHMM.digest.md    日报
```

---

## 目录结构

```
src/          Python 管线
  config.py       集中路径 + .env 加载（所有脚本 import 它）
  prompts.py      读取 prompts/ 里的 prompt 文件（一个用途一份）
  enrich_images.py / digest.py / watch_inbox.py   聊天入库与日报
  pulse.py        脉搏简报：切最近 N 分钟窗口 → Claude → STE 英语简报
  discord_post.py 把消息 POST 到 Discord 频道 Webhook（自动分片）
  signal_format.py 信号卡 → ASD-STE100 简化技术英语（静态模板）
  cycle.py        编排器：入库→脉搏简报→信号→推送（挂计划任务每 15min）
  tickers.py      标的宇宙 + 黑话/别名词典（ETP/ETF 为重点）
  extract.py      从聊天文本抽 ticker（正则 + 词典）
  market.py       yfinance 行情 / Finnhub 财报（带每日缓存）
  levels.py       关键位计算（前高低 / 均线 / VWAP / 量比）
  store.py        SQLite 落库（signals.db）
  signals.py      信号打分（analyze() 可被 cycle 复用）→ 信号卡 + 存库
prompts/      喂给 AI 的 prompt（一个用途一份，改措辞不用动代码）
  pulse_summary.md  脉搏简报（STE 英语）
  digest_system.md / digest_user.md / digest_merge.md  日报 prompt
userscript/   discord-digest-exporter.user.js（浏览器里跑，支持多频道轮流采集）
data/         运行时数据（inbox/ cache/ market_cache/ signals.db 不入库）
  inbox/        浏览器下载落地处，watcher 处理后归档到 processed/
  cache/        图片哈希缓存
  chats_by_date/  按日 + 按频道整理：<日>/<频道>/merged.enriched.txt
  chat_frank/     Frank 频道导出
  market_cache/   yfinance 行情当日缓存（可随时删，会自动重拉）
  signals.db      信号台账（SQLite，永久留存，见「信号打分」）
scripts/      register_task.ps1（一键注册 Windows 计划任务）
trade_notes/  分析与工具设计文档（含 MVP-v0-实施记录.md，讲自动化+数据源）
.env          你的 API key + Webhook（已 gitignore；从 .env.example 复制）
```

> 每 15 分钟自动导出 + 本地自动入库的完整流程见
> [trade_notes/MVP-v0-实施记录.md](trade_notes/MVP-v0-实施记录.md)。
> 每 15 分钟**自动推送简报+信号到你自己的 Discord 频道**见下面「自动推送」一节。

---

## 一次性准备

### 1. 浏览器脚本

装 [Tampermonkey](https://www.tampermonkey.net/)，新建脚本，把
`userscript/discord-digest-exporter.user.js` 的内容整段粘进去保存。

> 不建议直接往 Discord 的 F12 控制台粘代码。Discord 在控制台里放了一个
> 红色警告，是为了防止有人骗你粘恶意脚本——用 Tampermonkey 至少代码是
> 存在你自己这儿的。无论如何，粘任何脚本之前先自己读一遍。

打开脚本顶部的 `CFG` 可以改配置：

| 项 | 默认 | 说明 |
|---|---|---|
| `hoursBack` | 24 | 往回抓多少小时 |
| `limitByHours` | `true` | 导出时是否按 `hoursBack` 过滤；设为 `false` 导出全部已采集消息 |
| `autoScroll` | `false` | `false` = 被动模式，你自己滚，脚本只记录 |
| `scrollDelayMs` | 800 | 自动模式下每次滚动的等待时间，网慢就调大 |

**关于模式选择**：Discord 条款禁止用自动化手段访问服务。被动模式下滚动是你
自己做的，脚本只读取浏览器已经渲染给你看的内容，性质上更接近"复制粘贴"；
自动模式严格说仍在灰色地带，虽然服务端流量和真人滚动没有区别、检测面接近零。
建议默认用被动模式。

### 2. Python 环境

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API Key

把 `.env.example` 复制成 `.env`，填入你的 key（`.env` 已被 `.gitignore` 忽略，不会提交）：

```
ANTHROPIC_API_KEY=sk-ant-...
# 国内直连 api.anthropic.com 可能不通，需要代理：
# HTTPS_PROXY=http://127.0.0.1:7890
FINNHUB_API_KEY=...
FMP_API_KEY=...
FRED_API_KEY=...
POLYGON_API_KEY=...
```

所有脚本 `import config` 会自动加载 `.env`；也可临时用 `$env:ANTHROPIC_API_KEY="..."` 覆盖。

---

## 每天的操作

### 悬浮面板按钮说明

脚本跑起来后，右下角出现一个 **📥 聊天导出器** 小面板。从上到下：

| 元素 | 名字 | 作用 |
|---|---|---|
| 顶部计数 | `已采集 N 条消息` | 当前脚本被动记录到的消息数，会随你滚动实时增长 |
| 复选框 | `仅导出最近 N 小时` | 勾选=只导出最近 `CFG.hoursBack` 小时；**取消勾选=导出全部已采集消息**（不按时间裁剪） |
| 按钮 ① | `自动往上滚·补历史` | 脚本模拟往上滚动，把更早的历史消息也采集进来（被动模式下你也可以自己按 `PageUp` 慢慢滚） |
| 按钮 ② | `立即导出（下载文件）` | 立刻把已采集消息导出成 **3 个文件**（`.json` / `.txt` / `-images.txt`）到下载目录 |
| 按钮 ③ | `定时导出：关 / 开(每15分钟)` | 点一下开启，之后每 `CFG.autoExportMin` 分钟自动导出一次；**标签页要保持打开**，关掉就停 |
| 按钮 | `清空计数（重新采集）` | 清掉已采集缓存并从 0 重新计数，**不会**删除已下载的文件 |

> 每个按钮/复选框都带 **鼠标悬停提示（title）**，忘了含义时把鼠标移上去即可看到。
> 控制台里也可手动调用：`__digest.toggleAuto(true)` 开定时导出、`__digest.diagnose()` 排查选择器、`__digest.exportAll()` 手动导出。

### 走一遍

**第一步：导出。** 打开 `#tradingroom`，右下角出现小面板。往上滚到你想要的
起点（比如昨天这个时候），再滚回底部，面板计数会一路涨。点 **②立即导出**，
浏览器会下载三个文件。

如果你想导出"全部已抓取内容"（不按时间裁剪），把面板里的
**"仅导出最近 N 小时"** 取消勾选再点 **②立即导出**。

被动模式下滚动要慢一点——滚太快 Discord 来不及渲染，中间的消息会漏。
按住 `PageUp` 大概是合适的速度。

**第二步：处理图片。** 这一步要尽快做，Discord 的图片 URL 带签名、
**大约 24 小时后失效**。

```bash
python src/enrich_images.py discord-202607261830.txt
```

图片按内容哈希缓存在 `data/cache/` 下，同一张图（比如反复转发的那张）
只会调一次 API。不想花钱可以加 `--no-api`，只下载不转写。

**第三步：生成日报。**

```bash
python src/digest.py discord-202607261830.enriched.txt
```

排查问题时建议开启调试日志：

```bash
python src/digest.py discord-202607261830.enriched.txt --debug
```

`digest.py` 现在支持以下能力：

- `--debug`：把每次 API 调用的 `stop_reason`、输入/输出 token、内容块类型等写到
  `.digest.debug.jsonl`，用于定位空输出、截断等问题。
- `--debug-file <path>`：自定义调试日志文件路径。
- `--max-tokens <n>`：调整单次调用的输出上限（默认 8000）。
- 自动续写：如果某次返回 `stop_reason=max_tokens`，脚本会自动发起"继续"请求并拼接结果，
  避免生成半截日报。

得到 `.digest.md`，包含：一句话概览、拆开的话题线索（每条列出参与者、
正反论据、结论）、提到的标的表格、**黑话与术语注释**、以及哪些说法
是断言但没给论据。

---

## 自动推送（每 15 分钟：脉搏简报 + 最新信号 → 你自己的频道）

`cycle.py` 把整条链路串起来，每 15 分钟自动做一轮：

```
油猴脚本（标签页开着）──每15min──▶ data/inbox/discord-<频道>-<id>-<时间>.json
        │
        ▼  cycle.py --once（Windows 计划任务每15min触发）
  1. watch_inbox：去重合并 + enrich，按频道落到 chats_by_date/<日>/<频道>/
  2. 切最近 N 分钟窗口（两个频道合并）——窗口内没新消息就跳过，不烧钱不刷屏
  3. Claude(haiku) 生成脉搏简报（ASD-STE100 简化技术英语）
  4. 从当天讨论抽重点标的，重新打分，得到最新信号卡（STE 英语）
  5. 简报 + 信号卡拼成一条，POST 到 DISCORD_WEBHOOK_URL 指向的频道
```

推送内容整条用 **ASD-STE100 简化技术英语**（短句、常用词、现在时；股票代码/人名保持原样）。
简报走 Claude；信号解释是本地**静态 STE 模板**（免费、确定性）。

### 一次性准备

1. **建 Webhook**：进你自己的频道 → 频道设置 → 整合(Integrations) → Webhook → 新 Webhook → 复制 URL。
2. **填 .env**：把 URL 写进 `.env` 的 `DISCORD_WEBHOOK_URL=...`（`.env` 已 gitignore，别贴到公开场合）。
3. **注册计划任务**（每 15 分钟自动跑）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
   # 卸载： ... register_task.ps1 -Remove
   ```

   任务名 `DiscordDigestCycle`，`Start-ScheduledTask -TaskName DiscordDigestCycle` 可手动跑一次。

> 前提同「每天的操作」：那个 Discord 标签页要保持打开、油猴脚本开着「定时导出」，
> 负责把文件下到 `data/inbox/`。cycle 只管消费 inbox。

### 手动 / 自测

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

常用参数：`--minutes N`（窗口大小，默认 45）、`--limit N`（最多打分几个标的）、
`--event-today`（大宏观事件当天压绿灯）、`--always`（窗口空也照发）、`--no-save`（不写 signals.db）。

> 改推送措辞：脉搏简报的 prompt 在 [prompts/pulse_summary.md](prompts/pulse_summary.md)；
> 信号的 STE 模板在 [src/signal_format.py](src/signal_format.py)。
> 重点发言人名单在 [prompts/vip_speakers.txt](prompts/vip_speakers.txt)（一行一个，随时加）。

### 排查「推送暂停了一段时间」

推送靠两条腿：**① 油猴脚本每 15 分钟导出文件**、**② 计划任务每 15 分钟消费**。
任何一条停了都会出现「一段时间没有新推送」。先看两个日志：

```powershell
# cycle 每轮都会记：窗口多少条消息、推了还是跳过、为什么、有没有报错
Get-Content data\cycle.log -Tail 30
# watcher 记：inbox 里的导出有没有被处理
Get-Content data\inbox\watch.log -Tail 30
```

日志里常见结论与含义：

| 日志 | 含义 | 处理 |
|---|---|---|
| `结果：跳过（窗口内无新消息）` + `最近消息…分钟前` 很大 | **导出停摆**：油猴脚本没在产出文件 | 见下方「导出停摆」 |
| `结果：已推送 N 条` | 正常 | —— |
| `结果：本轮失败 …` + traceback | cycle 报错（多为 API/网络/Webhook） | 看 traceback；检查 `.env`、代理 |
| watch.log 有大段时间没有任何行 | 那段时间 inbox 没进新文件 = 导出停摆 | 见下方 |

**导出停摆最常见的两个原因**（都在浏览器/系统侧，不是脚本 bug）：

1. **Chrome 把后台的 Discord 标签页「丢弃/冻结」了**。长时间不看那个标签，浏览器会冻结甚至卸载它，定时器就停了。
   - 缓解：把 Discord 标签**固定(pin)**、尽量别最小化太久；
     或在 `chrome://discards` 里把 discord.com 设为不可丢弃；
     或用一个单独的窗口只放 Discord。
   - 脚本 v0.4 起，右下角面板会显示「上次导出 时间（N 分钟前）」，**超过 2 个周期没动静会标红 ⚠停摆?**，控制台也会告警——一眼就能看出是不是它停了。
2. **电脑睡眠**。睡眠时脚本和计划任务都会暂停。
   - 任务已设 `WakeToRun`（到点唤醒）+ 电池也运行；但**浏览器里的油猴定时器无法唤醒系统**，所以睡眠期间的消息仍会漏。
   - 盯盘时段建议把电源计划设为不睡眠。

> 长缺口（夜里几小时）通常就是关机/睡眠，属正常。要留意的是**盘中**的 1–3 小时缺口，一般就是上面第 1 条。


---

## 信号打分（盯盘辅助）

日报是"读懂群里在聊什么"；信号打分是"把群里点的票拉行情、按固定 checklist 打个分"，
帮你快速筛掉噪音、聚焦到值得盯的少数标的。**重点只跑 ETP/ETF**（个股默认只收录不打分）。

> ⚠️ 这是决策**辅助**，不是荐股。信号规则是 v0 粗版，务必自己复核；仓位/风险自负。

### 用法

```bash
# 1) 从当天群聊记录抽标的并打分（最常用）
python src/signals.py data/chats_by_date/20260803/merged.enriched.txt

# 2) 直接给 watchlist，绕过抽取
python src/signals.py --watchlist SOXL,SOXS,QQQ

# 3) FOMC / CPI / 非农 当天：把"宏观环境"标成不 clear（压绿灯）
python src/signals.py --watchlist SOXL --event-today
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--all` | 连个股/期货一起打分（默认只跑重点 ETP/ETF） |
| `--limit N` | 最多打分多少个标的（默认 12） |
| `--event-today` | 当天有大宏观事件，环境不 clear |
| `--no-save` | 只看不写库（调试用） |

### 怎么读信号卡

```
── SPY (etf · 标普500) ──  🟡黄灯  档位:B
   现价 758.02  建议入场 755.58  建议止损 732.91
   • 关键位突破(量不足)[B] 收758.02 上破20日高755.58，量0.7773×
   checklist: 1_环境clear=✓  2_A档信号=✗  3_入场点位明确=✓  4_止损位明确=✓
   ema9=745.01 ema21=744.41 sma50=744.60 vwap=754.93 vol_ratio=0.78
```

- **灯**：🟢绿=有 A 档信号 + 环境 clear + 有止损（可重点看）；🔴红=无信号或全 C 档（跳过）；🟡黄=其余（需人工判断）。
- **档位**：`A`（高置信）> `B`（需确认）> `watch`（临近）> `—`（无）。
- **checklist 前 4 条**：环境是否 clear、是否有 A 档信号、入场点位是否明确、止损位是否明确——四项越全越可动手。
- **建议入场/止损**：目前只在"关键位突破"时给（入场=20 日高，止损=入场×0.97）；其它信号留空，需你自己定。
- **最后一行**：ema9/21、sma50、VWAP、量比（vol_ratio<1 缩量、>1 放量），供你核对。

### v0 只做 3 类信号

| 信号 | 触发 | 档位 |
|---|---|---|
| **关键位突破** | 收盘上破 20 日高 | 量比≥1.2 → **A**；量不足 → **B**；临近(≥99%) → watch |
| **财报催化** | Finnhub 最近财报（仅个股） | beat → **A**；miss → **C** |
| **利空不跌** | 近 5 日单日暴跌后守住低点、未创新低（纯价格代理） | 一律 **B**「未确认」，阈值按杠杆放大（3x 需 -12%、2x 需 -8%、1x 需 -4%） |

另有 `reclaim 50 日线`（B 档）作为辅助。

### 数据怎么存

- **`data/signals.db`（SQLite，永久）**——核心"信号→结果"台账。表：`runs`（每次运行）、
  `mentions`（当天被点名的票+次数+原文样本）、`signals`（每张信号卡）、`outcomes`（预留，回填 T+1/3/5 实际涨跌做复盘）。这是要长期沉淀、值得备份的东西。
- **`data/market_cache/`（每日 CSV，可丢弃）**——yfinance 行情缓存，已 gitignore，删了自动重拉。

快速查看已存的信号：

```bash
python -c "import sqlite3;c=sqlite3.connect('data/signals.db');[print(r) for r in c.execute('select ts,ticker,tier,light,price from signals order by id desc limit 20')]"
```

### 已知局限（v0，别当精确工具用）

1. **行情拉的是"今天"的实时数据**，不是聊天那天——所以请**当天盘中/盘后跑**；拿历史聊天回测会时间错位。
2. **利空不跌是纯价格代理**（没接新闻），只当 B 档参考、"需多日确认"，别当天 all in。
3. **宏观环境目前靠手动 `--event-today`**，还没接 FOMC/CPI 日历。
4. **期权大单方向**（sell put 是主买主卖）需付费源，尚未接入——期权信息只当 B 档确认。

---

## 成本

一天几百条消息的精简文本大约几千到一万 token。用 `claude-sonnet-5` 做日报，
单次成本很低。图片转写默认走 `claude-haiku-4-5-20251001`，更便宜，
且有缓存不会重复计费。想要更好的图表理解可以：

```bash
python src/enrich_images.py in.txt --model claude-sonnet-5
```

---

## 出问题时

**导出的作者名全是 `(unknown)`，或者一条都抓不到**
Discord 的 CSS class 名是 hash 过的，改版会失效。在 F12 控制台运行：

```js
__digest.diagnose()
```

会打一张表，哪一项是 0 就是哪个选择器坏了。修复方法：在 Discord 页面里右键
一条消息 → 检查，看看新的属性名，改脚本里对应的 `[class*="..."]`。
用 `id^=` 开头的那几个（`message-content-`、`chat-messages-`）比较稳定，
基本不会变。

**图片下载报 403 / 404**
签名过期了。重新导出一次，或者用 JSON 文件里保留的消息 ID 回到 Discord 手动看。
养成导出后立刻跑 `enrich_images.py` 的习惯。

**消息有缺漏**
滚动太快。降低速度重滚一遍，脚本按 message ID 去重，重复采集不会产生重复条目。
也可以不点"清空"，分几次滚完再一起导出。

**API 连不上**
检查 `HTTPS_PROXY`。`requests` 和 `anthropic` SDK 都读这个环境变量。

**日报看起来没写完（末尾半句话/半张表）**
先用 `--debug` 重跑，查看 `.digest.debug.jsonl` 里是否出现
`"stop_reason": "max_tokens"`。脚本会自动续写；如果仍连续触发上限，
请提高 `--max-tokens`，或减少输入规模后再生成。

---

## 后续计划

- 结构完整性检查（计划中）：生成后自动校验关键章节是否齐全（如"一句话概览"、
  "话题线索"、"提到的具体标的与事件"、"黑话与术语注释"、"值得追问的地方"）。
  若缺失章节，再自动补一次请求。这个功能后续再加。

---

## 可以自己改的地方

- **`enrich_images.py` 里的 `TRANSCRIBE_PROMPT`**：现在是文字截图逐字提取、
  图表简述、表情包直接标记为无关。如果群里图表变多，可以让它多说一点读数。
- **`digest.py` 里的 `PROMPT`**：章节结构直接改这里。比如你只想要术语注释
  不要话题摘要，删掉对应段落即可。
- **`SYSTEM` 里对你自己的描述**：现在写的是"有量化金融背景，数学不用解释，
  但对交易黑话和板块叙事不熟"。等你熟了之后把这句改掉，输出会更精简。
- **积累术语表**：跑一段时间后，把 digest 里的术语注释汇总成一个固定文件，
  在 `PROMPT` 里作为已知词汇传进去，让模型只解释新出现的词。
