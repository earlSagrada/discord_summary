# MVP v0 实施记录 —— 15min 自动入库 + 数据源测试

> 目标（本轮）：把 [盯盘工具设计](盯盘工具设计.md) 里 **MVP v0 的「数据入库」这一段** 真正跑起来：
> 1. 群聊导出 **自动化到每 15 分钟一次**（约束：Discord 上不能用油猴，只能手动粘贴 JS）；
> 2. 其它数据源 **实测哪些免费可用 / 哪些要 key / 哪些付费**，付费的标出来给你之后测；
> 3. 记录整个流程（本文）。
>
> ⚠️ 本文 Part A/B 记录 **数据流水线** 那一轮；信号打分 / checklist 引擎已在后续补齐，见 **Part C**。
> ⚠️ digest 按你的决定 **不自动跑**（每次都调 Claude 太贵），只自动 `enrich`，digest 手动/每日批量。

---

## Part A · 群聊导出自动化到每 15min

### A.0 约束与思路
- **约束**：Discord 封油猴/扩展注入，你只能把 JS **手动粘贴进浏览器 console** 跑。
- **思路**：粘贴一次 → 脚本 **自己 `setInterval` 每 15min 导出一次** → 文件下载到本地 `data/inbox/` → 本地 **watcher** 自动 `enrich` 入库。
- **为什么用 Blob 下载而不是直接 POST 到本地**：Discord 的 CSP 会挡掉 `fetch('http://localhost...')`，而 `Blob + a.click()` 下载不受 CSP 限制，最稳。

### A.1 已做的改动（`discord-digest-exporter.user.js`）
| 改动 | 说明 |
|---|---|
| `CFG.hoursBack: 24 → 1` | 自动导出用 **1h 小窗口**，减少每次重叠（停机 >1h 会漏，见 A.5） |
| 新增 `CFG.autoExportMin: 15` | 自动导出间隔（分钟） |
| 新增面板按钮 **「自动导出:关/开」** | 点一下开启/关闭 `setInterval(exportAll, 15min)` |
| `exportAll({silent})` | 自动触发时不弹 alert、走 console 日志 |
| `window.__digest.toggleAuto(true/false)` | 也可在 console 手动开关 |

> 每次导出仍是 3 个文件：`discord-<stamp>.json` / `.txt` / `-images.txt`（stamp = UTC 到分钟）。
> watcher 只认 `.json`（含 message id，最适合去重），另两个会被一起归档。

### A.2 一次性设置（你来做，只做一次）
1. **Chrome 下载目录改到 data/inbox**：设置 → 下载内容 → 位置改成
   `c:\Users\bojan\Desktop\discord-chat\data\inbox`，并 **关掉**「下载前询问每个文件的保存位置」。
2. **允许自动下载多个文件**：第一次自动导出时 Chrome 会弹「此网站尝试下载多个文件」→ 点 **允许**（只弹一次）。

### A.3 每次盯盘的操作（粘贴一次）
1. 打开目标 Discord 频道，F12 → Console，粘贴整个 `discord-digest-exporter.user.js` 内容，回车。
2. 右下角面板出现，点 **「自动导出:关」** 变成 **「自动导出:开(15m)」**。
3. **让这个标签页开着**（passive 模式靠它收新消息，见 A.5 坑）。之后每 15min 自动下 3 个文件到 `data/inbox/`。

> 首次想把过去 24h 也补进来：粘贴前先在 console 改 `CFG.hoursBack = 24` 手动点一次「导出」，再改回自动。

### A.4 本地 watcher（`src/watch_inbox.py`）
监听 `data/inbox/`，把每次导出的 `.json` 按 **message id 去重合并成「每天一份」**，再自动调 `enrich_images.py` 补图片转写。**不跑 digest**。

产物写到 `data/chats_by_date/<YYYYMMDD>/`：
| 文件 | 内容 |
|---|---|
| `merged.json` | 去重后原始记录（id 唯一、按时间排序）—— 去重的真值源 |
| `merged.txt` | 压缩文本（格式同油猴脚本，enrich/digest 直接吃） |
| `merged.enriched.txt` | 图片转写回填后的文本 —— **每日 digest 就跑这个** |

跑法（二选一）：
```powershell
# 常驻：开着一个终端一直盯 data/inbox/
.\.venv\Scripts\python.exe src\watch_inbox.py

# 或挂 Windows 计划任务，每 15min 触发一次，处理完就退出
.\.venv\Scripts\python.exe src\watch_inbox.py --once
```
- 去重逻辑对齐油猴 `harvest()`：同一条消息保留 **信息更全**（有作者 / 图片更多 / 正文更长）的那份 → 重叠的 1h 窗口不会产生重复。
- `enrich_images.py` 自带 **sha256 图片缓存**：每天反复 enrich，只有 **新图片** 才花 API 钱，旧图命中缓存。
- 已冒烟测试通过（合并、作者向前填充、reply/embed/图片清单、enrich 回填均正确）。

### A.5 完整链路
```
Discord 标签页(开着) ──粘贴JS+开自动导出──▶ 每15min 下载 discord-*.{json,txt,images}
        │                                             │
        │                                             ▼
        │                                   data/inbox/ ──src/watch_inbox.py──▶ 去重合并 + enrich
        │                                             │
        │                                             ▼
        │                        data/chats_by_date/<日>/merged.enriched.txt
        │                                             │
        └────────────────── 每日手动/批量 ───────────▶ python src/digest.py …merged.enriched.txt
```

### A.6 三个坑（务必知道）
1. **标签页必须开着**：passive 模式靠 `MutationObserver` 收新消息；关掉标签页那段时间的消息 **不会** 被采集（1h 窗口也补不回 >1h 的空档）。要长时间盯盘，就让这个页保持打开、最好别最小化太久。
2. **后台标签页节流**：Chrome 会节流后台标签的定时器，但 15min 间隔远大于节流阈值，**不影响** 自动导出；只是 passive 采集在后台会慢一点。
3. **ToS 灰区**：任何对 Discord 的自动化抓取都属灰区（README 已注明）。当前方案是「你人在场、手动粘贴、被动记录」，比无头自动化温和，但仍请自担风险、别公开分发。

---

## Part B · 数据源实测结果

> 测试环境：项目 `.venv`（`requests` + 新装 `yfinance`）。✅=本轮实测可用，❗=需免费 key（留给你测），💰=付费（留给你之后测），⚠️=脚本里不稳/被挡。

### B.1 ✅ 免费、本轮实测可用
| 源 | 拿到什么 | 实测 | 对应 checklist 需求 |
|---|---|---|---|
| **yfinance**（Yahoo）分钟 OHLCV | `Ticker.history(period=1d,interval=1m)` → 115 行 OHLCV+Volume | ✅ | **P0.1 分钟级行情+量**（缩量/放量/VWAP） |
| **yfinance 期权链** | `option_chain()` → calls/puts 含 `strike/volume/openInterest/impliedVolatility/bid/ask` | ✅ 55×2 | **P1.5 期权 positioning**：IV、OI 墙（put wall）、成交量集中度 |
| **Yahoo chart 原始接口** | `query1.finance.yahoo.com/v8/finance/chart/{sym}` → 200 | ✅ | 同上，免依赖备用 |
| **Yahoo 港股 7709.HK** | 同 chart 接口，`interval=1d` → HKD 报价 200 | ✅ | **P0.4 联动标的**（7709） |
| **SEC EDGAR** | `data.sec.gov/submissions/CIK…json` → 全部 8-K/PR/10-Q 列表 200（需 `User-Agent` 头） | ✅ | **P1.6 催化新闻·最高可信度来源**（公司官方披露） |
| **Polymarket** | `gamma-api.polymarket.com/markets` → 事件概率 200 | ✅ | **P1.7 宏观事件博弈定价**（regime filter） |
| **CoinGecko** | `/simple/price` → BTC 现价 200 | ✅ | 加密/风险偏好参考 |

> ⚠️ **数据延迟**：Yahoo 美股 intraday 近实时（部分交易所延迟 ~15min），期权是延迟报价。**够 MVP 打分/盯盘，不够 tick 级精确入场**——真要实时看 Part B.3 的 Polygon/Tradier。
> ⚠️ **关键价位（P0.2 前高低/均线/VWAP/整数关口）没有现成源**：直接用上面的 OHLCV **自己算**，不需要额外数据源。

### B.2 ❗ 免费但要注册 key（留给你测）
| 源 | 免费额度 | 补的缺口 |
|---|---|---|
| **Finnhub** | 免费 key，60 req/min；earnings calendar、basic financials | **P0.3 财报 consensus vs actual**、财报日历 |
| **Financial Modeling Prep (FMP)** | 免费 key，250 req/日；earnings calendar + estimates | 同上，作 Finnhub 交叉验证 |
| **Alpha Vantage** | 免费 key，25 req/日（很少）；earnings、EPS | 备用，额度紧 |
| **FRED**（St. Louis Fed） | 免费 key；利率/CPI/失业等宏观时间序列 | **P1.7 宏观日历/环境** |

> 建议顺序：先测 **Finnhub**（额度最舒服）拿财报日历+实际值；FRED 拿宏观。key 我不能替你申请，注册后把 key 放环境变量、我再帮你接。

### B.3 💰 付费（留给你之后测）
| 源 | 提供 | 为什么要它 |
|---|---|---|
| **Unusual Whales** | options **flow / sweeps / 大单方向** | **A 档信号 4「巨量 sell put」的方向** —— yfinance 只给 OI/IV，给不了「这笔是买是卖」 |
| **ORATS** | 期权 IV surface / greeks / flow | 更专业的 IV/skew/GEX |
| **Polygon.io** | 实时行情 + 期权 trades（逐笔） | 真·实时 + 期权逐笔方向；免费档无实时 |
| **Tradier**（开户） | 期权链带 greeks、较实时；部分随账户免费 | 实时期权链的平价替代 |
| **CBOE DataShop** | 官方期权/GEX 数据 | 权威 gamma/GEX |

> 结论：**期权「资金结构 positioning」（OI/IV/put wall）用 yfinance 免费就能做**；但 **「大单方向 flow」（sell put 是主买还是主卖）必须付费**（Unusual Whales / Polygon options trades）。这正是设计文档 §5 坑 3「大单≠方向」要小心的地方——没有付费 flow 前，期权信号只当 B 档确认，别当方向。

### B.4 ⚠️ 脚本里不稳 / 被挡
| 源 | 现象 | 处理 |
|---|---|---|
| **Nasdaq earnings API** (`api.nasdaq.com/api/calendar/earnings`) | 脚本请求 **超时**（挡非浏览器 UA） | 别依赖它，用 Finnhub/FMP 代替 |
| **Stooq CSV** | 脚本拿到 404（限流/需浏览器） | 浏览器能开，脚本不稳；yfinance 已覆盖，暂不用 |
| **Yahoo 期权原始接口** | 直连 401「Invalid Crumb」（要 cookie+crumb） | 用 **yfinance**（它自动处理 crumb），已验证可用 |
| **CME FedWatch** | 无官方免费 API | 用 **Polymarket** 概率替代，或从 fed funds futures 自算 |

---

## Part C · 现状小结 & Next

**本轮打通的（v0 前半段·数据入库）**
- ✅ 群聊：粘贴一次 → 每 15min 自动导出 → watcher 去重合并 + 自动 enrich → 每天一份 `merged.enriched.txt`。
- ✅ 行情/期权 positioning/港股联动/官方催化/宏观博弈定价：**免费源实测可用**，接入路径清楚。
- 🅿️ 财报实际值(P0.3)、宏观时序：**要免费 key**，你注册后我接。
- 💰 期权大单方向 flow：**付费**，你之后测。

**新增打通的（v0 后半段·信号打分引擎）—— 2026-08-03**

已把「关键位计算 + 3 类信号 + checklist 前 4 条 + 信号卡 + 落库」跑通，端到端验证 OK。
6 个模块在 `src/`：

| 模块 | 职责 |
|---|---|
| `tickers.py` | 标的宇宙 `UNIVERSE` + 黑话/别名词典；ETP/ETF 为 focus，个股/期货只收录；杠杆 ETP 带 `lev` |
| `extract.py` | 从 `merged.enriched.txt` 抽 ticker（`$cashtag` + 英文别名词边界 + 中文子串；2 字母票只经 cashtag/中文） |
| `market.py` | yfinance 日线/分钟线（每日 CSV 缓存）+ Finnhub 财报 |
| `levels.py` | 关键位：20 日高低 / ema9·21 / sma50·200 / VWAP / 量比 |
| `store.py` | SQLite `data/signals.db`：`runs / mentions / signals / outcomes` |
| `signals.py` | 主程序：抽标的 → 拉数据 → 算位 → 打分 → 打印信号卡（🟢🟡🔴）→ 存库 |

跑法（详见 README「信号打分」节）：

```powershell
# 从当天聊天抽重点标的(ETP/ETF)打分
.\.venv\Scripts\python.exe src\signals.py data\chats_by_date\20260803\merged.enriched.txt --limit 12
# 或直接指定 watchlist
.\.venv\Scripts\python.exe src\signals.py --watchlist SOXL,SOXS,QQQ
# 大宏观事件当天压绿灯
.\.venv\Scripts\python.exe src\signals.py --watchlist SOXL --event-today
```

**数据持久化决策**：
- `data/signals.db`（SQLite，**永久**）= 核心「信号→结果」台账，值得备份；`outcomes` 表已建好，预留 B4 回填。
- `data/market_cache/`（每日 CSV，**可丢弃**）= yfinance 行情缓存，已 gitignore，删了自动重拉。

**本轮跑通中修的问题**：
- yfinance 偶发 NaN 尾行（尤其港股 7709.HK、未收盘）→ `market.get_daily` 丢弃 NaN Close 行，现价不再为空。
- Windows 控制台 cp1252 遇 ★/emoji 崩溃 → `config.py` 统一把 stdout/stderr 切 UTF-8。
- **利空不跌对杠杆 ETP 过敏感**（3x 日常 ±16% 也触发）→ 阈值按 `lev` 放大（3x 需 -12%、2x 需 -8%、1x 需 -4%）。修后 NVDL(2x,-6.8%) 不再误报，SOXL(3x,-16%)、SOXX(1x,-5.4%) 仍正确触发。
- `--limit` 只跑 focus 标的时输出未说明 → 现在打印「跳过 N 个个股/期货，加 --all 可纳入」。

**信号规则（v0 粗版，重在跑通闭环 + 积累「信号→结果」）**
- 关键位突破：上破 20 日高，量比≥1.2 → A，量不足 → B，临近 → watch。
- 财报催化（仅个股，Finnhub）：beat → A，miss → C。
- 利空不跌（纯价格代理）：一律 B「未确认」，阈值按杠杆放大。
- reclaim 50 日线 → B（辅助）。
- 打分：checklist 前 4 条（环境 clear / A 档信号 / 入场明确 / 止损明确）→ 灯 🟢(A+环境clear+止损)/🔴(无信号或全C)/🟡；档位 A>B>watch>—。

**信号引擎已知局限（v0）**
- 行情拉「今天」的实时数据，非聊天当日 → 当天跑；历史聊天回测会错位。
- 利空不跌无新闻标签，只当 B 档确认。
- 宏观环境仅手动 `--event-today`，未接 FOMC/CPI 日历。
- 期权大单方向未接（需付费源），期权只当 B 档确认。

**Next（v0 收尾，待你确认再做）**
1. ~~写「关键位计算模块」~~ ✅ 已做（`levels.py`）。
2. ~~只做 3 类信号 + checklist 前 4 条 → 信号卡~~ ✅ 已做（`signals.py`）。
3. 接 Finnhub 财报日历+actual（key 已在 `.env`）：目前 `market.get_last_earnings` 已接 Finnhub earnings，可再验证 A/C 档在真实财报日的表现。
4. **模块 6「信号→结果」回填（B4）**：新写 `backtest.py`，把 `signals` 表里的历史信号按 T+1/3/5 实际涨跌填进 `outcomes`，开始统计胜率——**这是下一步最该做的**，否则永远不知道信号准不准。
5. 词典补漏：把跑出来的未收录 cashtag（如 `$SPCX`、`$SKHY`）按需加进 `tickers.py`。

**要你拍板/去做的事**
- [ ] 改 Chrome 下载目录到 `data/inbox/` + 允许多文件下载（A.2）。
- [ ] 决定 watcher 跑法：常驻终端 还是 Windows 计划任务 `--once`。
- [x] 注册 **Finnhub** 免费 key（已放 `.env`，`market.py` 已接 earnings）。
- [ ] 是否愿意为「期权大单方向 flow」付费（Unusual Whales / Polygon）——不付的话期权只做 B 档确认。
- [ ] 拍板下一步是否做 **B4 结果回填 `backtest.py`**（回填 `outcomes`，统计胜率）。

---

## Part D · 每 15 分钟自动推送（脉搏简报 + 信号 → 自己的频道）—— 2026-08-11

**目标**：把"读懂群在聊什么"和"最新信号"每 15 分钟自动送到用户自己的 Discord 频道，
全程免手动。语言用 **ASD-STE100 简化技术英语**（整条推送都是英文，代码/人名保留原样）。

**几个已定决策**
- 推送方式：**Discord Webhook**（`.env` 里 `DISCORD_WEBHOOK_URL`）。CSP 不挡出站 webhook，最稳。
- 简报窗口：**滚动最近 45 分钟**（可调），不是当天全量 → 便宜、不重复。
- 频道范围：tradingroom + frank **合并成一段**统一简报。
- 触发：**Windows 计划任务**每 15min 跑 `cycle.py --once`（`scripts/register_task.ps1` 一键注册）。
- 省钱护栏：窗口内**无新消息就整轮跳过**（不调 AI、不推送）。
- 信号解释：本地**静态 STE 模板**（`signal_format.py`），只有脉搏简报走 Claude(haiku)。

**这轮新增/改动的模块**

| 文件 | 职责 |
|---|---|
| `prompts/`（新目录） | 喂给 AI 的 prompt 一个用途一份；`pulse_summary.md` 是 STE 脉搏简报；digest 的 system/user/merge 也外置过来 |
| `src/prompts.py` | 读 prompts/ 的小加载器（带缓存） |
| `src/pulse.py` | 从 enriched 文本按 UTC 时间切"最近 N 分钟"窗口（多频道合并）→ Claude(haiku) 出 STE 简报；窗口空返回空串 |
| `src/discord_post.py` | 把文本 POST 到 Webhook，>1900 字自动按行分片，429 限流重试 |
| `src/signal_format.py` | 信号卡 → STE 英语（🟢🟡 详列入场/止损，🔴 只统计一句） |
| `src/cycle.py` | 编排器 `--once`：watcher → 切窗口 → 简报 → `signals.analyze()` → 拼装 → 推送；`--dry-run/--no-watch/--anchor last` 便于离线自测 |
| `src/signals.py` | 抽出可复用的 `resolve_from_text()` / `score_symbol()` / `analyze()`，CLI 与 cycle 共用；`build_card` 增加 `sig_objs/metrics/env_clear` 供 STE 渲染 |
| `src/watch_inbox.py` | **按频道分目录**落地：解析 `discord-<频道>-<id>-<时间>.json` 的频道名 → `chats_by_date/<日>/<频道>/merged.*`（旧无标签文件归 `misc/`） |
| `scripts/register_task.ps1` | 一键注册/卸载 Windows 计划任务 |

**冒烟测试（已过）**
- `cycle.py --dry-run --no-watch --anchor last --date 20260803`：窗口切片、STE 简报、STE 信号卡、消息拼装全部正常。
- `watch_inbox` 频道路由：`discord-frank-…-….json` → `20260803/frank/merged.*`，`channel_of()` 各种文件名解析正确。
- `discord_post.split_message`：长文本按行分片、空串返回空、单行超长硬切均正确。

**衔接说明 / 已知点**
- watcher 现在**按频道分目录**，旧的扁平 `chats_by_date/<日>/merged.*` 仍被 cycle 兼容读取（flat 兜底）。
- 信号每 15min 会往 `signals.db` 写一条 run（96 条/天）——这是有意的台账；嫌多可给 cycle 加 `--no-save`。
- 依赖那个 Discord 标签页保持打开（被动采集）——和 Part A 一样的前提。
- 尚未做：宏观事件日历自动判断 `--event-today`（仍手动）、`outcomes` 回填（B4）。

---

## Part E · 第三阶段：数据闭环 + pulse 深化 —— 2026-08-16

**目标**：把信号从"一个裸价位"升级成"带上下文 + 新鲜度 + 风险刻度"，并接成
"信号→环境→结果→复盘"的闭环；再加一个周期复盘程序沉淀"谁值得听、什么方法赚钱"。

**这轮新增/改动**

| 文件 | 职责 |
|---|---|
| `src/levels.py` | 新增 `breakout_age()`：现价站上 20 日高的突破是几天前发生的 + 从突破日至今涨幅 |
| `src/extract.py` | 抽出 `_line_hits()` 复用；新增 `last_mention_times()`（按时间块记录每票最后被提及时间）|
| `src/signals.py` | 新增 `staleness()` 新鲜度/priced-in 指标；🟢 命中过期标签自动降 🟡；卡片带 `target/env_reason/freshness`；`analyze/score_symbol` 接 `now/mention_times/event_names` |
| `src/signal_format.py` | 信号卡语义化：Entry/Stop 写明含义、风险%、2R 目标、现价 vs 入场关系、priced-in 告警、环境不 clear 原因 |
| `prompts/pulse_summary.md` | 增加催化 A/B/C 分级、priced-in 检查段、未证实 KOL 噪音标注 |
| `src/events.py`（新） | 宏观事件日历：FRED 发布日期 + 静态 FOMC 表 + Finnhub/FMP → `is_event_today()`；`cycle/signals` 自动置 env_clear |
| `event_calendar.json`（新） | 用户维护的 FOMC/自定义大事件日期表 |
| `src/store.py` | 新增 `add_outcome/existing_outcomes/all_signals/outcome_rows` |
| `src/backtest.py`（新） | 回填 `outcomes`(T+1/3/5) + 胜率统计（按信号名/档位/灯色）+ 调参建议（报告级，人工确认）|
| `src/review.py`（新） | 周期复盘：最近 N 天聊天 + 胜率 → sonnet 出中文报告；写 `trade_notes/reviews/`，TL;DR 推 Discord，VIP 建议写 `prompts/vip_suggestions.md` |
| `prompts/weekly_review.md`（新） | 周期复盘 prompt |
| `scripts/register_maintenance_tasks.ps1`（新） | 每日回测 + 每周复盘 两个计划任务 |
| `src/config.py` | 新增新鲜度/回测阈值常量（`EXTENSION_PCT_MAX` 等）+ `EVENT_CALENDAR` 路径 |

**冒烟测试（已过）**
- `events.py --date 2026-01-28` → 命中 FOMC；普通日返回空。
- `backtest.py --backfill --report` → 对既有 `signals.db` 回填 399 条 outcome，胜率统计合理
  （财报 beat T+1 100%、关键位突破确认 T+1 93%、关键位突破量不足 13% ← 印证"量不足要降级"）。
- `signals.py --watchlist NVDA` → 旧财报 beat 触发 `earnings_consumed`，🟢 正确降 🟡。
- `cycle.py --once --dry-run --no-watch --anchor last --date 20260813` → 脉搏简报含催化分级/priced-in，
  信号卡语义化 + 新鲜度告警，事件模块正常。
- `review.py --no-api` → prompt 拼装（含聊天 + 胜率统计）正常。

**已知点 / 后续**
- FOMC 静态表是按公开日程填的，请每年核对更新；FRED release 名称/ID 匹配是 best-effort。
- Finnhub/FMP 经济日历免费额度常受限（403），当前作为可选交叉校验，静默降级。
- 回测早期样本小，命中率仅供参考；调参一律"先建议、后人工确认"，不自动改权重。

---

*本文只覆盖 MVP v0 的数据入库与数据源盘点。信号/打分引擎见 [盯盘工具设计](盯盘工具设计.md) §2–§4，方法论见 [交易信号复盘与方法总结](交易信号复盘与方法总结.md)。*

---

## Part G · 历史区间 pulse + 期权数据融入信号验证 —— 2026-08-16

**目标**：(1) 能回看过去任意时段出 pulse；(2) 把期权数据融入信号验证。

**数据源调研（重要）**
- 期权：**yfinance `option_chain` 是唯一可靠免费源**（strike/bid/ask/volume/OI/IV 齐全）。
  **Polygon 免费档期权 403 未授权**；Finnhub/FMP 期权付费。→ 全部走 yfinance。
- 免费数据**拿不到成交方向**（主买/主卖）→ 期权只当 **B 档确认/上下文**，绝不当触发器。

**这轮新增/改动**

| 文件 | 职责 |
|---|---|
| `src/pulse.py` | 新增 `combine_range()` / `gather_range_texts()` + CLI `--last`（90m/6h/3d）/`--from`/`--to`/`--post`；跨度大自动放大输出上限。历史 pulse 触发是 **CLI**（只有出站 webhook、无入站 bot）|
| `src/options.py`（新） | yfinance 期权链 → `option_metrics()`：ATM IV、IV skew、P/C(OI+量)、call wall(阻力)/put wall(支撑)、异常量 strike（方向未知）；per-day JSON 缓存；任何失败都返回 None 不抛错 |
| `src/signals.py` | `score_symbol` 拉期权指标传入 `build_card`；新增 `_mark_options_capped`：突破入场紧贴上方 call wall → tag `capped` + 🟢 降 🟡（并入既有 `_STALE_FLAGS` 降级）；`print_card` 加一行期权 |
| `src/signal_format.py` | 信号卡渲染期权确认块（STE 英语）：IV / P·C / 支撑阻力 / 异常量（方向未知）/ capped 提示 / IV 过高提示 |
| `src/config.py` | 新增 `OPTIONS_ENABLED` / `IV_HIGH=0.60` / `CALL_WALL_CAP_PCT=1.5` |

**冒烟测试（已过）**
- `options.py NVDA/SOXL/7709.HK` → 个股/3x ETP 指标合理（SOXL IV 116% 天然高、P/C 2.4）；HK 无期权优雅返回 None。
- `signals.py --watchlist NVDA,SOXL,QQQ` → 卡片含期权行，NVDA/QQQ 入场紧贴 call wall 触发 `capped`。
- `format_cards([score_symbol("NVDA")])` → STE 期权块完整（IV/墙/异常量/capped）。
- `pulse.py --from 20260813 --to 20260814 --no-api`（1078 条）/`--last 3d`（1806 条）/参数守卫均正常；真实 API 出中文 STE 简报。
- `cycle.py --dry-run` 全链路（含期权）跑通。

**已知点**
- 免费期权无成交方向，"巨量大单"只能标"方向未知"；3x ETP 的 IV 天然偏高（IV_HIGH 只做提示）。
- 期权只减分/提示，从不加分（不会把 🟡 抬成 🟢），对齐方法论"期权只做 B 档确认"。
- 历史 pulse 触发是命令行，可绑热键/计划任务；无法在 Discord 里打命令触发（缺入站 bot）。

---

## Part H · 两周运行复盘：故障排查与修复 —— 2026-08-29

对 08-14~08-29 的 7771 行 `cycle.log`（927 跳过 / 282 推送 / 98 失败）+ signals.db 做体检，
发现 4 个真问题并已修复。

**① ANTHROPIC_API_KEY 失效，且失败是"静默"的（最严重）**
- 08-28 06:15 起连续 98 轮 `AuthenticationError 401`，最后一次成功推送停在 08-27 20:45。
- 根因：key 失效（**需用户自行重建 key**，代码无法修）。
- **真正的产品缺陷**：`cycle.py` 只有"导出停摆"告警，**异常失败只写日志、不通知**
  → 推送"消失"了一天半都没人知道。
- 修复：新增 `maybe_alert_failure()`——任何一轮异常都往 Discord 发
  `🛑 The pulse job failed.`（按错误类型去重、成功推送后复位；401 会附
  "请重建 ANTHROPIC_API_KEY" 的针对性提示）。

**② 绿灯"灭绝"：08-17 起 1361 条信号 0 个绿灯**
- 根因：Finnhub `stock/earnings` 返回的是**上一季**财报，永远是几十天前
  → `earnings_consumed` 标签对几乎每只个股恒定触发
  → 因为它在 `_STALE_FLAGS` 里，把**任何**卡片的 🟢 都降成 🟡（连"新鲜突破"的票也误杀）。
- 修复：过期财报**在 detect() 里直接降为 C 档**（`财报 beat[已消化]`，不再给 A 档资格），
  同时把 `earnings_consumed` 移出 `_STALE_FLAGS`（避免二次惩罚）。
  验证：新鲜突破+旧财报 → 恢复 🟢；只有旧财报 → 🔴；旧突破(3天) → 仍 🟡。真实数据 NVDA 回到 🟢。

**③ 财报"年龄"算错：用了财季结束日而非发布日**
- `period` 是**财季结束日**，甚至可能是未来日期（NVDA 显示 `-32天前`）。
- 修复：`market.last_earnings_date()` 从 earnings 日历取**真实发布日**，
  `get_last_earnings` 带 `report_date`，staleness 优先用它。NVDA 现在正确显示"3天前"。

**④ 回测统计严重虚高：同一信号被重复计 96 次**
- cycle 每 15 分钟给同一只票记一条，同一天最多 96 条完全一样的记录，
  统计时全当独立样本 → "关键位突破确认 T+1 胜率 98.2% (n=111)" 是假的。
- 修复：`backtest._dedupe()` 按「票×天×信号组合×持有期」去重。
  去重后 **3847 → 309 条真实样本**，该信号实为 n=6、66.7%。

**⑤ 维护任务从未注册**
- `DiscordBacktestDaily` / `DiscordWeeklyReview` 用户没跑过注册脚本 → 回测积压 1498 条未回填、
  周报两周没产出。已执行 `register_maintenance_tasks.ps1` 注册，并手动回填 3448 条 outcome。

---

## Part I · 推送改中文 + 磁盘清理 + 文档拆分 —— 2026-08-29

**① 推送格式改造（按用户反馈）**
- 之前是 ASD-STE100 简化技术英语，用户反馈**太长、读起来累、句子太生硬**。
- 现在全部改**中文、说人话、逻辑连贯**：
  - `Priced-in risk.` → `⚠️ 存在 priced-in 风险：突破是 2 天前的事了，现价已比进场位高出 4.2%
    ——这波多半已被市场消化，现在追进去容易接盘`
  - 信号卡数字压成一行：`现价 225.16 ｜ 进场 227.23（20日高）｜ 止损 220.41（-3.0%）｜ 目标 240.87`
  - 黑话顺手解释一次；一张卡 3~6 行。
- **排版强制化**：新增 `pulse.tighten()` 对 AI 输出后处理——`# 标题`→`**标题**`、
  **删掉所有空行**、去行尾空白。不靠模型自觉，格式永远一致。
- prompt 重写为中文并限制 **500 字以内**；`signal_format.py` 整体重写为中文模板。
- 停摆告警、失败告警、env_reason、历史 pulse 头部也一并中文化。

**② 分级磁盘清理 `src/cleanup.py`**
- 体检发现：`data/inbox` 里混进一个 **109.5 MB 的无关文件**（Chrome 下载目录指向这里），
  `cache/images` 20.8 MB 原图、`market_cache` 423 个按天缓存都没人清。
- 分级保留（阈值在 `config.py`）：`inbox/processed` 15 天、`market_cache` 3 天、
  `cache/images` 7 天、日志超 5 MB 保留末 2000 行并轮转到 `.1`。
- **永不删**：`chats_by_date/`（核心语料）、`signals.db`（台账）、
  `cache/transcripts.json`+`url_index.json`（**花过钱的图片转写结果**）、
  `_processed.json`（watcher 去重状态，删了会重复处理全部历史）。
- **默认 dry-run**，`--apply` 才真删；`data/inbox` 根目录的大文件只**提醒**不删。
- 已验证：沙盒测试确认过期文件被删、`_processed.json` 即使过期也受保护；
  当前可清理 768 个文件 / 29.8 MB。已注册周日 22:00 的 `DiscordCleanupWeekly`。

**③ README 拆分成 docs/**
- README 从 **535 行压到 117 行**（概览 + 快速开始 + 文档索引 + 常用命令）。
- 明细拆成 `docs/` 11 个文件共 1100 行：`setup` / `architecture` / `pulse` / `signals` /
  `priced-in` / `options` / `macro-events` / `backtest` / `review` / `troubleshooting` + 索引。
- 每个文件带返回索引的面包屑；69 个内部链接全部校验通过、0 坏链。

---

## Part J · 增量脉搏 + 每日自我复盘闭环 —— 2026-08-29

用户反馈两个问题：**一天之内看 pulse 感觉同样的事被说很多遍，看不出变化和趋势**；
以及**没有复盘和自我改进的机制**——应该从群里讨论和 Frank 的 Substack 复盘里
学分析方法，还要记录谁用什么方法做对了交易。

**① `src/cleanup.py` 表格对齐**
- 中文是全角字符，`f"{s:<18}"` 按**字符数**补位，中文列必然错位。
- 加 `_w()`（East Asian Width 为 W/F 的算 2 列）+ `_pad()` 按显示宽度补空格。

**② 增量脉搏：只讲「和上一轮比变了什么」**
变化拆成两层，因为两种变化的可信来源完全不同：
- **确定性层 `src/delta.py`**（不调 AI）：灯色变化、今天首次进榜、讨论升温、价格进展。
  数字必须算出来，让模型猜就会编。
  - `snapshot()` 必须在 `signals.analyze(save=True)` **之前**调用，否则本轮自己的
    记录会污染"上一轮"的基准 —— 这是最容易踩的坑，`cycle.py` 里排在第 3b 步。
  - 提及热度用「本轮累计 − 上轮累计」：cycle 每轮对当天全文抽标的，
    `mentions.count` 本来就是当天累计值，两轮相减正好是这段时间的新增讨论。
  - 去噪：**一票只出一条**（评级 > 首次 > 升温 > 价格），红灯/无信号的新票不报
    "首次进榜"——否则变化块本身又变成了新的重复源。
- **叙事层**：新增 `store.pulse_rounds` 表记下每轮**实际推出去**的正文和「今日主线」，
  下一轮把最近 3 轮塞进 prompt 的「今天已经推送过的内容」，第一条规则就是
  "说过的事除非有新进展，一个字都不要再写"。
- prompt 输出结构改为：🆕 新增了什么 / 📈 旧话题有什么进展 / 🧊 是否已消化 /
  🧭 今日主线（一行，20 字内，`pulse.extract_thread()` 抠出来存库）/ ⚠️ 存疑。
- `tighten()` 增强：段落标题（🆕📈🧭… 开头且非 bullet）自动加粗 —— 实测模型
  时加时不加，靠自觉会导致每条消息长得不一样。
- 实测效果：同一批消息第二轮推送时，「🆕 新增了什么」自动变成"暂无新话题"，
  内容被正确挪到「📈 旧话题有什么进展」。

**③ Substack PDF 解析 `src/substack.py`**
- **踩的坑**：第一版导出的 PDF 字体没有 ToUnicode 映射，PyMuPDF/pypdf 取出的文本
  **丢掉全部英文和数字**——「10Y 收 4.736%」变成「收」。这种残缺文本喂给 AI
  只会产出幻觉。用户换了导出方式后英文数字占比 24.8%，正常。
- 所以 `extract()` 做质量门禁：英文数字占比 < 5% 直接抛错，**宁可跳过也不喂残缺文本**。
- 顺带去掉 Substack 每页重复的页脚/页码噪音；解析结果缓存成同名 `.txt`。

**④ 每日自我复盘 `src/selfreview.py` + `prompts/playbook.md`**
- 和 `review.py`（每周、对外看群）的分工：selfreview **对内**，只回答
  "今天我们哪里分析得不够好"。
- 六份材料：今天聊天 / **今天我们自己推的简报** / 今天信号+历史兑现 /
  **Frank 的 Substack 复盘** / 当前 playbook / 当前 VIP 名单。
  材料 2 是关键——没有它模型只能复述新闻，有了它才能对照"当时怎么说的 vs 本该怎么说"。
- 报告五段：谁说对了**及其推理路径** / 可以沉淀的方法 / 我们分析的不足 /
  明天重点关注（带验证条件）/ VIP 建议。
- **闭环**：「可以沉淀的方法」自动写回 `prompts/playbook.md` 的 `BEGIN/END AUTO` 区间，
  新条目插最前面，上限 20 条，挤出的落到「已淘汰」；`pulse.summarize()` 每轮取
  前 8 条注入 prompt。playbook 变强 = 明天每一条简报都变强。
- VIP 名单仍然**只提建议不自动改**（写 `vip_suggestions.md`），保留人工闸门。
- 首次实跑（复盘 08-27）质量很高，模型自己指出了一个真实缺口：
  "我们的信号系统只做了发射，没有做验证闭环"，并新增 3 条方法进 playbook。

**⑤ 调度与文档**
- `register_maintenance_tasks.ps1` 增加 `DiscordSelfReviewDaily`，每天 **23:45**——
  排在 23:30 的回填之后，这样复盘能看到当天信号的兑现结果。四个任务已全部注册成功。
- 新增 `docs/delta-pulse.md`、`docs/self-review.md`；更新 `docs/README.md`、
  `docs/architecture.md`、根 README；`requirements.txt` 加 `pymupdf`。

---

## Part K · 自我优化闭环 + VIP 自动化 + Substack 触发 —— 2026-08-31

用户提出：**让程序和 AI 自己优化现有方法，但必须记录优化了什么、为什么、
以及为什么不采取其他方法**；可以用贵一点的模型，之后只需人工过目。
另外 Substack 每周一更新，希望**检测到新文件就自动跑复盘 + 优化**。

**① 可调参数注册表（`config.TUNABLES` + `data/tunables.json`）**
这是让 AI 改自己的前提——护栏必须先立好：
- 7 个阈值进注册表，各带上下界和说明；AI **只能写 JSON，永远碰不到 Python 源码**。
- `_load_tunables()` 把 JSON 叠在出厂默认上；坏值**跳过并告警**，绝不让一个
  错误配置把整条管线带崩。
- 越界值自动夹到边界并标注；未知参数名直接拒绝。
- **删掉 tunables.json 就整体回到出厂默认** —— 天然可回滚。
- 前置确认：所有模块都是 `config.X` 调用时读取，不是 import 时捕获，覆盖层才生效。

**② `src/optimize.py` 自我优化（opus）**
和 selfreview 的分工：selfreview 改**分析方法**（今天怎么看盘），
optimize 改**系统本身**（阈值、信号权重、方法库排序）。
- prompt 强制每条改动写四件事：依据（必须引具体数字）、预期效果、
  **否决的替代方案**、风险。另有「继续观察」和「**本次没做的事**」两段，
  让用户能区分"想过但否决了"和"根本没想到"。
- 样本量纪律写进 prompt：`n < 15` 不许用来调参。
- playbook 支持 add / remove / **promote**——因为 pulse 只注入前 8 条，
  排在第 12 位的方法等于没写，**提权和新增同样重要**。
- 首跑结果验证了纪律有效：**提出 0 项参数调整**，理由是
  "EARNINGS_STALE_DAYS=3 刚好对齐财报衰减窗口，改成 2 天会误伤仍有效的 T+1 信号"。
  并在「本次没做的事」里点出自己的边界："改成只推 T+1 是信号逻辑层面的改动，
  超出参数调整范围"。
- 它还自己暴露了一个真实盲点："材料中没有看到他在当前 VIP 名单里，需要确认名单内容"
  —— 确实漏喂了，已补 `vips_text()` 进材料五。

**③ `src/vips.py` VIP 名单自动化**
回答"怎么确认、能不能自动化"：
- `--apply` 全采纳｜`--auto` 只采纳**连续 2 期**都被建议的｜`--add/--remove` 手动｜`--undo` 撤回。
- 为什么要连续 2 期：单期建议常来自一两句话的印象，而这份名单决定 pulse 重点关注谁，
  影响很大。两期都提说明是稳定观察。
- **踩到的解析坑**：标题「建议观察（暂不**加入**）」被误判成 add——
  因为"暂不加入"里含"加入"。修法是加 `_SKIP_HEADS`（观察/暂不/维持/无需…）
  并**先于** add/remove 判断。复盘的原意恰恰是"先别加"，判反了会自动加错人。
- 计数只统计**真正需要动手**的建议：已在名单的人再被建议加入、不在名单的人被建议移除，
  都是空操作，不占计数。

**④ 新 Substack PDF 落地即自动校准**
- `substack.new_posts()` / `mark_seen()` **读写分离**：解析或复盘中途失败就不标记，
  下轮自动重试。要是检测时就标记，一次抖动会让一期内容被永久漏掉。
- `substack_pipeline.py` 五步：解析（**质量不合格直接中止**，绝不拿残缺文本改系统）
  → 周期复盘 → 自我优化 → VIP 自动落地 → 标记已处理。
- 触发：`cycle.py` 每轮顺手检查（列目录 + 读小 JSON，开销可忽略），
  发现新文件就 **spawn 后台进程**——流水线要跑几分钟，
  15 分钟的脉搏推送绝不能为它等着。
- **真实端到端验证**：今天到的 `20260831.pdf` 触发全流程成功，约 3 分钟跑完
  （复盘 → 优化 2 项方法库调整 → VIP 无变更因未达门槛 → 标记）；再跑一次正确跳过。

**⑤ 调度与文档**
- 新增 `DiscordOptimizeWeekly`（周日 21:00，排在周复盘之后）作为**兜底**——
  万一某周 Substack 没更新也会优化一次。现共 6 个计划任务。
- 新增 `docs/optimization.md`；更新 scheduled-tasks / architecture / backtest
  （补「回填结果谁在用」）/ docs 索引 / 根 README。

