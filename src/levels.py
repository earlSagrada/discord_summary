"""关键价位计算：前高低 / 均线 / VWAP / 整数关口 / 量能。"""

import math

import pandas as pd


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) else round(v, 4)
    except (TypeError, ValueError):
        return None


def _round_levels(price: float) -> list[float]:
    """离现价最近的整数关口（步长随价位自适应）。"""
    step = 1 if price < 20 else 5 if price < 100 else 10 if price < 500 else 50
    base = math.floor(price / step) * step
    return [round(base - step, 2), round(base, 2), round(base + step, 2), round(base + 2 * step, 2)]


def _vwap_today(intraday: pd.DataFrame) -> float | None:
    if intraday is None or intraday.empty:
        return None
    idx = pd.to_datetime(intraday.index)
    last_day = idx[-1].date()
    day = intraday[idx.date == last_day]
    if day.empty or "Volume" not in day or day["Volume"].sum() == 0:
        return None
    tp = (day["High"] + day["Low"] + day["Close"]) / 3
    return _f((tp * day["Volume"]).sum() / day["Volume"].sum())


def compute_levels(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict:
    if daily is None or daily.empty:
        return {}
    daily = daily[daily["Close"].notna()]  # yfinance 有时带 NaN 尾行（HK/未收盘）
    if daily.empty:
        return {}
    close = daily["Close"]
    prior = daily.iloc[:-1]  # 排除当日，算"前高/前低/均量"
    last = _f(close.iloc[-1])
    prev_close = _f(close.iloc[-2]) if len(close) > 1 else None

    n = len(close)
    levels = {
        "last": last,
        "prev_close": prev_close,
        "chg_pct": _f((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else None,
        "high_20": _f(prior["High"].tail(20).max()) if len(prior) else None,
        "low_20": _f(prior["Low"].tail(20).min()) if len(prior) else None,
        "prev_high": _f(prior["High"].iloc[-1]) if len(prior) else None,
        "prev_low": _f(prior["Low"].iloc[-1]) if len(prior) else None,
        "ema9": _f(close.ewm(span=9, adjust=False).mean().iloc[-1]),
        "ema21": _f(close.ewm(span=21, adjust=False).mean().iloc[-1]),
        "sma50": _f(close.rolling(50).mean().iloc[-1]) if n >= 50 else None,
        "sma200": _f(close.rolling(200).mean().iloc[-1]) if n >= 200 else None,
        "avg_vol_20": _f(prior["Volume"].tail(20).mean()) if len(prior) else None,
        "today_vol": _f(daily["Volume"].iloc[-1]),
        "vwap": _vwap_today(intraday),
    }
    if last:
        levels["round_levels"] = _round_levels(last)
        if levels["avg_vol_20"] and levels["today_vol"]:
            levels["vol_ratio"] = _f(levels["today_vol"] / levels["avg_vol_20"])
    return levels
