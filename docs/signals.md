> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 信号打分：关键位与财报怎么分析

信号打分做的事很朴素：**把群里点到的票拉行情，按一套固定规则打分**，
帮你把噪音筛掉、只盯少数值得看的标的。

> ⚠️ 这是决策**辅助**，不是荐股。规则是 v0 粗版，务必自己复核；仓位/风险自负。

设计原则来自 [交易信号复盘与方法总结](../trade_notes/交易信号复盘与方法总结.md)：
**右侧确认 > 左侧预测**——用"关键位 + 条件触发"代替"猜方向"，不到位就不动手。

---

## 一、关键位（key levels）怎么算

代码在 [`src/levels.py`](../src/levels.py) 的 `compute_levels()`，输入是 yfinance 日线 + 5 分钟日内数据。

### 算出来的位

| 字段 | 含义 | 怎么来的 |
|---|---|---|
| `last` / `prev_close` / `chg_pct` | 现价 / 昨收 / 涨跌幅 | 日线收盘 |
| **`high_20` / `low_20`** | **前 20 日最高/最低** | **排除当日**的前 20 根 bar（这是最核心的"关键位"）|
| `prev_high` / `prev_low` | 昨日高/低 | 前一根 bar |
| `ema9` / `ema21` | 9 / 21 日指数均线 | 收盘价 EWM |
| `sma50` / `sma200` | 50 / 200 日均线 | 数据够长才算，否则 None |
| `vwap` | 当日成交量加权均价 | 日内 5 分钟 bar：`Σ(典型价×量)/Σ量` |
| `avg_vol_20` / `today_vol` / **`vol_ratio`** | 20 日均量 / 今日量 / **量比** | `vol_ratio = 今日量 / 20日均量` |
| `round_levels` | 最近的整数关口 | 步长随价位自适应（<20 用 1、<100 用 5、<500 用 10、否则 50）|
| **`days_since_breakout`** | **突破是几天前发生的** | 见下面「突破年龄」 |

> **为什么 `high_20` 要排除当日**：如果把今天算进去，"今天创新高"就永远等于
> "今天 = 20 日高"，突破判定会自我实现、永远触发不了。排除当日后，
> `last > high_20` 才是真正的"站上了前期高点"。

### 突破年龄（`breakout_age`）

这是**判断信号是否已被 price in 的关键输入**。它从最新一根 bar 往回数，
看现价连续多少天保持在"各自前 20 日高"之上：

- 今天根本没突破 → `(None, None)`
- 今天**刚**突破 → `days_since_breakout = 0`（最新鲜）
- 连续站上 N 天 → `days_since_breakout = N-1`，并给出从最早那根突破日至今的涨幅 `move_since_breakout`

### 由关键位产生的信号

在 [`src/signals.py`](../src/signals.py) 的 `detect()` 里：

| 信号 | 触发条件 | 档位 |
|---|---|---|
| **关键位突破确认** | `收盘 > 20日高` **且 `量比 ≥ 1.2`** | **A** |
| **关键位突破(量不足)** | `收盘 > 20日高` 但量比 < 1.2 | **B** |
| **接近关键位** | `收盘 ≥ 20日高 × 0.99`（差 1% 以内） | watch |
| **reclaim 50 日线** | 昨收在 sma50 下方、今收回到上方 | **B** |
| **利空不跌[未确认]** | 近 5 日内单日暴跌后守住那天的低点、且未创新低 | **B**（一律标"需多日确认"）|

**"量"为什么这么重要**：破位没有量 = 没人真的在买，多半是假突破。
回测数据也印证了这点——见 [回测统计](./backtest.md)，`关键位突破(量不足)` 的
T+1/T+3/T+5 收益持续为负。

**利空不跌的阈值按杠杆放大**：暴跌门槛是 `-4% × 杠杆倍数`
（3x ETP 需要 -12%、2x 需要 -8%），避免杠杆产品的日常波动误触发。

### 入场 / 止损 / 目标怎么给

只有在出现"关键位突破"类信号时才给点位（其它信号留空，需你自己定）：

```
入场 entry  = high_20            （突破位本身，即"站上这里才算数"）
止损 stop   = entry × 0.97       （入场下方 3% = 你的最大风险）
目标 target = entry + (entry-stop) × 2   （约 2R，风险的 2 倍）
```

信号卡里会把这些**翻译成人话**，而不是只丢三个数字：

```
Entry 227.23 = the 20-day high (the breakout level).
Stop 220.41 = 3.00% below entry (your maximum risk).
Price 225.16. The price is 0.9% below the entry. The signal is not triggered yet.
Target near 240.87 (about 2 times the risk).
```

---

## 二、财报（earnings）怎么分析

代码在 [`src/market.py`](../src/market.py)，数据源是 **Finnhub**。只有个股有财报，ETF/ETP 一律跳过。

### 拿两类数据

**1. 最近一季的 actual vs estimate**（`get_last_earnings`）
调 `stock/earnings`，拿到 `actual`（实际 EPS）、`estimate`（市场预期）、
`surprisePercent`（超预期百分比）。

**2. 真实发布日**（`last_earnings_date`）
这一步很关键。Finnhub 的 `period` 字段是**财季结束日**，不是**财报发布日**——
它甚至可能是未来日期（NVDA 曾算出"-32 天前"这种荒谬结果）。
所以我们另外调 `calendar/earnings` 取过去 180 天里最近一次**已公布**（`epsActual` 非空）
的日期，作为这条催化的"真实年龄"。

> 这是 2026-08-29 修掉的一个真实 bug，详见 [MVP 实施记录 Part H](../trade_notes/MVP-v0-实施记录.md)。

### 财报信号的档位：**新鲜度决定一切**

| 情况 | 信号 | 档位 |
|---|---|---|
| beat（超预期）且**发布 ≤ 3 天** | `财报 beat` | **A**（真实业务催化）|
| beat 但**已过 3 天** | `财报 beat[已消化]` | **C**（仅作背景）|
| miss（不及预期） | `财报 miss` | **C** |

**为什么过期的 beat 要降到 C 档**：Finnhub 返回的永远是"最近一季"，
对大多数股票来说那是几十天前的旧闻——市场早就消化完了。
如果让它一直挂 A 档，等于每只个股常年顶着"A 档催化"，绿灯就失去意义了。

> 这正是 08-17 到 08-29 期间"绿灯灭绝"（1361 条信号 0 个绿灯）的根因，已修复。
> 关键设计点：**过期财报是失去 A 档资格，而不是把整张卡降级**——
> 否则一只"今天刚放量突破"的票会被它无关的旧财报连累。

### 财报还会影响"环境"

`score_symbol()` 里另有一条：如果该股 **2 天内有财报**（`upcoming_earnings_within`），
则把 `env_clear` 置为 False——财报前是典型的"环境不 clear"，
不该重仓押方向（对齐 checklist 第 1 条）。

---

## 三、灯色、档位与 checklist

### 档位（tier）
`A`（高置信）> `B`（需确认）> `watch`（临近）> `—`（无信号）。
取该票所有信号里的最高档。

### checklist（前 4 条）
```
1_环境clear     今天没有大宏观事件、也没有临近财报
2_A档信号       至少有一个 A 档信号
3_入场点位明确   有 entry
4_止损位明确     有 stop
```

### 灯色
```
🟢 绿灯 = 有 A 档信号  且  环境 clear  且  有止损
🔴 红灯 = 没有任何信号，或全部只有 C 档
🟡 黄灯 = 其余（需人工判断）
```

**再加一道"过期检查"**：即使算出绿灯，只要命中 priced-in 类标签
（追高 / 旧突破 / 聊天已冷 / 上方有期权盖子），**🟢 会保守降成 🟡**。
详见 [priced-in 判定](./priced-in.md)。

---

## 用法

```powershell
# 从当天群聊记录抽标的并打分
.\.venv\Scripts\python.exe src\signals.py data\chats_by_date\20260803\tradingroom\merged.enriched.txt

# 直接给 watchlist
.\.venv\Scripts\python.exe src\signals.py --watchlist SOXL,NVDA,QQQ

# 只看不写库
.\.venv\Scripts\python.exe src\signals.py --watchlist NVDA --no-save
```

| 参数 | 作用 |
|---|---|
| `--all` | 连个股/期货一起打分（默认只跑重点 ETP/ETF）|
| `--limit N` | 最多打分多少个标的（默认 12）|
| `--event-today` | 手动强制标为大宏观日（`events.py` 已自动判断，这是覆盖）|
| `--no-save` | 不写入 `data/signals.db` |

---

## 相关文档

- [priced-in 判定](./priced-in.md)——信号是不是已经被市场消化了
- [期权数据确认](./options.md)——期权墙/IV 怎么给信号做二次验证
- [宏观事件日历](./macro-events.md)——环境 clear 与否怎么自动判断
- [回测统计](./backtest.md)——这些信号到底准不准
