> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 期权数据确认

期权层只做确认和风险提示。它告诉你支撑、阻力、定价拥挤度；不告诉你方向。

## 数据源结论

| 数据源 | 结论 |
|---|---|
| yfinance `option_chain()` | 唯一可靠免费源；当前统一使用它 |
| Polygon 免费档期权 | 403 未授权 |
| Finnhub / FMP 期权 | 付费功能 |

免费期权数据拿不到成交方向。因此“巨量期权成交”只能标为方向未知，不能当触发器。

## 使用原则

- 期权只做 **B 档确认 / 上下文**。
- 期权墙只给支撑阻力，不给方向。
- 期权只减分或提示，从不加分。
- 方向仍由价格、量能、财报催化、宏观环境等主信号决定。

## 指标含义

| 指标 | 含义 |
|---|---|
| ATM IV | 最近到期、接近平值的隐含波动率 |
| IV skew | 约 5% OTM put IV 减 5% OTM call IV |
| Put/Call OI | put 未平仓量 / call 未平仓量 |
| Put/Call volume | put 成交量 / call 成交量 |
| call wall | 现价上方 call OI 最大的行权价，视为阻力 |
| put wall | 现价下方 put OI 最大的行权价，视为支撑 |
| unusual | `volume > OI` 且成交量大，表示今天有新仓位；方向未知 |

异常成交阈值在代码里是 `UNUSUAL_MIN_VOLUME = 500`。

## 如何影响信号

`signals.py` 会在 `score_symbol()` 中拉 `options.option_metrics()`，再把结果传给信号卡。

| 情况 | 影响 |
|---|---|
| 入场位上方很近有 call wall | 打 `capped` 标签；若原本是 🟢，保守降为 🟡 |
| ATM IV ≥ `IV_HIGH` | 提示“期权贵、市场可能已预期大波动” |
| 有 put/call wall | 只显示支撑 / 阻力 |
| 有 unusual volume | 显示 strike、call/put、成交量；方向未知 |

默认阈值在 `src\config.py`：

| 配置 | 默认 | 说明 |
|---|---:|---|
| `OPTIONS_ENABLED` | `True` | 是否启用期权确认 |
| `IV_HIGH` | `0.60` | IV ≥ 60% 提示期权贵 |
| `CALL_WALL_CAP_PCT` | `1.5` | call wall 距突破入场位 1.5% 内触发 `capped` |

> ⚠️ **3x/2x 杠杆 ETP 的 IV 天然就高**（如 SOXL 常在 100%+），会长期触发"IV 高"提示。
> 对这类标的，该提示只说明"期权本来就贵"，不代表异常。判断时请和同类标的横向比，
> 别拿它跟个股的 25% 直接对比。

推送里的期权块类似：

```text
Options: IV is 25%. P/C OI is 0.85. Support (put wall) is near 190.00. Resistance (call wall) is near 230.00.
Unusual option volume at 227.50 call (73756 lots). This is new. The direction is not sure.
Note: a big call wall is just above the entry. The breakout may stop there.
```

## 缓存与自测

期权指标按天缓存到：

```text
data\market_cache\<SYMBOL>_options_<YYYY-MM-DD>.json
```

自测：

```bash
python src\options.py NVDA
```
