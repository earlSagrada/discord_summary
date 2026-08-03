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

*本文只覆盖 MVP v0 的数据入库与数据源盘点。信号/打分引擎见 [盯盘工具设计](盯盘工具设计.md) §2–§4，方法论见 [交易信号复盘与方法总结](交易信号复盘与方法总结.md)。*
