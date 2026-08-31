> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 宏观事件日历

宏观事件只做 regime filter：决定今天要不要下场、给多大仓。不用它押方向。

## 数据源

`src\events.py` 会合并三个来源，任一命中就算今天有高影响宏观事件。

| 来源 | 内容 | 说明 |
|---|---|---|
| FRED release dates | CPI、非农、PPI、PCE、GDP、零售等 curated 高影响 release | 需要 `FRED_API_KEY` |
| `event_calendar.json` | 用户维护的静态 FOMC / 自定义大事件表 | FOMC 用决议日；每年核对更新 |
| Finnhub / FMP 经济日历 | 高影响美国事件交叉校验 | best-effort；无 key、403 或额度受限则静默跳过 |

静态表当前长这样：

```json
{"date": "2026-01-28", "name": "FOMC rate decision", "impact": "high"}
```

## 命中后的效果

`cycle.py` / `signals.py` 每轮会自动调用 `events.event_names()`。

命中后：

- 该轮信号的环境标为不 clear。
- 推送头部加提示：

```text
Macro today: … The market is not clear. Trade small.
```

- 信号卡里也会写明环境原因。

`--event-today` 仍保留，作为手动覆盖：

```bash
python src/signals.py --watchlist SOXL --event-today
```

## 缓存

事件结果按天缓存，避免 15 分钟轮询打爆免费额度：

```text
data\market_cache\events_<YYYY-MM-DD>.json
```

如需忽略缓存，`events.py` 支持 `--force`。

## 自测

```bash
python src\events.py                 # 看今天命中哪些事件
python src\events.py --date 2026-01-28
```

## 维护提醒

`event_calendar.json` 是人工维护表。请每年核对官方 FOMC 日程后更新，尤其是下一年的决议日。
