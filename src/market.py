"""行情/财报数据层。

- yfinance：日线 + 日内 bar（带按天的本地 CSV 缓存，避免反复打接口）。
- Finnhub：个股财报 actual vs estimate（beat/miss）+ 未来财报日期（env-clear 用）。
ETF/ETP 没有财报，earnings 相关函数对它们返回空。
"""

import datetime as dt
import os

import pandas as pd
import requests
import yfinance as yf

import config

CACHE = config.DATA_DIR / "market_cache"
CACHE.mkdir(parents=True, exist_ok=True)
FINNHUB = "https://finnhub.io/api/v1"


def _cache_file(sym: str, interval: str) -> "os.PathLike":
    today = dt.date.today().isoformat()
    safe = sym.replace("=", "_").replace(".", "_")
    return CACHE / f"{safe}_{interval}_{today}.csv"


def _load_cache(path) -> pd.DataFrame | None:
    if path.exists():
        try:
            return pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            return None
    return None


def get_daily(sym: str, period: str = "1y") -> pd.DataFrame:
    path = _cache_file(sym, "1d")
    cached = _load_cache(path)
    if cached is not None and not cached.empty:
        return cached
    df = yf.Ticker(sym).history(period=period, interval="1d")
    if not df.empty:
        df = df[df["Close"].notna()]  # 去掉 yfinance 偶发的 NaN 尾行
        df.to_csv(path)
    return df


def get_intraday(sym: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    path = _cache_file(sym, interval)
    cached = _load_cache(path)
    if cached is not None and not cached.empty:
        return cached
    df = yf.Ticker(sym).history(period=period, interval=interval)
    if not df.empty:
        df.to_csv(path)
    return df


def get_last_earnings(sym: str) -> dict | None:
    """最近一季 actual vs estimate（beat/miss）。ETF 返回 None。

    注意：Finnhub `stock/earnings` 的 `period` 是**财季结束日**（可能还是未来日期），
    不是**财报发布日**。判断"这条催化多新鲜"必须用发布日 → 这里补一个 `report_date`
    （来自 earnings 日历），拿不到时才退回 period。
    """
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(f"{FINNHUB}/stock/earnings",
                         params={"symbol": sym, "token": key}, timeout=15)
        if not r.ok:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        e = data[0]  # 最新一季
        return {
            "period": e.get("period"),
            "report_date": last_earnings_date(sym),
            "actual": e.get("actual"),
            "estimate": e.get("estimate"),
            "surprise": e.get("surprise"),
            "surprisePercent": e.get("surprisePercent"),
        }
    except requests.RequestException:
        return None


def last_earnings_date(sym: str, lookback_days: int = 180) -> str | None:
    """最近一次**已发布**财报的公布日期（YYYY-MM-DD）。拿不到返回 None。"""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    today = dt.date.today()
    try:
        r = requests.get(f"{FINNHUB}/calendar/earnings",
                         params={"from": (today - dt.timedelta(days=lookback_days)).isoformat(),
                                 "to": today.isoformat(),
                                 "symbol": sym, "token": key}, timeout=15)
        if not r.ok:
            return None
        payload = r.json()
        cal = payload.get("earningsCalendar", []) if isinstance(payload, dict) else []
    except (requests.RequestException, ValueError):
        return None
    dates = [c.get("date") for c in cal
             if c.get("symbol") == sym and c.get("date") and c.get("epsActual") is not None]
    return max(dates) if dates else None


def upcoming_earnings_within(sym: str, days: int = 2) -> bool:
    """未来 days 天内是否有该标的财报（env-clear 判定）。拿不到就当没有。"""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return False
    today = dt.date.today()
    try:
        r = requests.get(f"{FINNHUB}/calendar/earnings",
                         params={"from": today.isoformat(),
                                 "to": (today + dt.timedelta(days=days)).isoformat(),
                                 "symbol": sym, "token": key}, timeout=15)
        if not r.ok:
            return False
        cal = r.json().get("earningsCalendar", []) if isinstance(r.json(), dict) else []
        return any(c.get("symbol") == sym for c in cal)
    except requests.RequestException:
        return False
