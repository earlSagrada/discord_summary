"""期权数据层：yfinance option_chain → 当日期权确认指标。

只缓存计算后的指标，不缓存原始期权链。期权墙/IV 只做 B 档确认：
它们提示支撑/阻力和定价拥挤，不给交易方向。
"""

import argparse
import datetime as dt
import json
import math

import pandas as pd
import yfinance as yf

import config

CACHE = config.DATA_DIR / "market_cache"
CACHE.mkdir(parents=True, exist_ok=True)

NEAR_EXPIRY_DAYS = 35
UNUSUAL_MIN_VOLUME = 500


def _cache_file(sym: str):
    today = dt.date.today().isoformat()
    safe = sym.replace("=", "_").replace(".", "_")
    return CACHE / f"{safe}_options_{today}.json"


def _empty_metrics(sym: str, spot=None) -> dict:
    return {
        "atm_iv": None,
        "iv_skew": None,
        "pc_oi": None,
        "pc_vol": None,
        "call_wall": None,
        "put_wall": None,
        "unusual": [],
        "front_expiry": None,
        "spot": _f(spot),
    }


def _load_cache(path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _empty_metrics(data.get("ticker", ""), data.get("spot"))
            for k in base:
                if k in data:
                    base[k] = data[k]
            if not isinstance(base.get("unusual"), list):
                base["unusual"] = []
            return base
    except Exception:
        return None
    return None


def _save_cache(path, metrics: dict) -> None:
    try:
        path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _f(x, ndigits: int = 4) -> float | None:
    try:
        v = float(x)
        return round(v, ndigits) if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _ratio(num, den) -> float | None:
    num, den = _f(num), _f(den)
    if num is None or den in (None, 0):
        return None
    return round(num / den, 4)


def _parse_expiry(exp: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(exp)[:10])
    except ValueError:
        return None


def _num_col(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _sum_col(df: pd.DataFrame, col: str) -> float:
    s = _num_col(df, col)
    return float(s.fillna(0).sum()) if not s.empty else 0.0


def _spot_from_ticker(ticker) -> float | None:
    try:
        fast = ticker.fast_info
        for key in ("last_price", "regular_market_price", "previous_close"):
            val = _f(fast.get(key) if hasattr(fast, "get") else getattr(fast, key, None))
            if val:
                return val
    except Exception:
        pass
    try:
        hist = ticker.history(period="5d", interval="1d")
        if hist is not None and not hist.empty:
            close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            if not close.empty:
                return _f(close.iloc[-1])
    except Exception:
        pass
    return None


def _nearest_iv(df: pd.DataFrame, target: float, *, below=None, above=None) -> float | None:
    if df is None or df.empty or target is None:
        return None
    work = df.copy()
    work["strike"] = _num_col(work, "strike")
    work["impliedVolatility"] = _num_col(work, "impliedVolatility")
    work = work[work["strike"].notna() & work["impliedVolatility"].notna()]
    if below is not None:
        work = work[work["strike"] <= below]
    if above is not None:
        work = work[work["strike"] >= above]
    if work.empty:
        return None
    idx = (work["strike"] - target).abs().idxmin()
    return _f(work.loc[idx, "impliedVolatility"])


def _front_metrics(calls: pd.DataFrame, puts: pd.DataFrame, spot: float | None) -> tuple[float | None, float | None]:
    if spot is None:
        return None, None
    call_iv = _nearest_iv(calls, spot)
    put_iv = _nearest_iv(puts, spot)
    ivs = [x for x in (call_iv, put_iv) if x is not None]
    atm_iv = round(sum(ivs) / len(ivs), 4) if ivs else None

    put_otm = _nearest_iv(puts, spot * 0.95, below=spot)
    call_otm = _nearest_iv(calls, spot * 1.05, above=spot)
    skew = round(put_otm - call_otm, 4) if put_otm is not None and call_otm is not None else None
    return atm_iv, skew


def _add_wall(walls: dict[float, float], df: pd.DataFrame, spot: float | None, *, above: bool) -> None:
    if spot is None or df is None or df.empty:
        return
    strikes = _num_col(df, "strike")
    oi = _num_col(df, "openInterest").fillna(0)
    for strike, open_interest in zip(strikes, oi):
        strike = _f(strike)
        open_interest = _f(open_interest)
        if strike is None or open_interest is None or open_interest <= 0:
            continue
        if (above and strike > spot) or (not above and strike < spot):
            walls[strike] = walls.get(strike, 0.0) + open_interest


def _wall(walls: dict[float, float]) -> float | None:
    if not walls:
        return None
    strike, _ = max(walls.items(), key=lambda item: item[1])
    return _f(strike, 2)


def _unusual_rows(df: pd.DataFrame, side: str, expiry: str) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    strikes = _num_col(df, "strike")
    volume = _num_col(df, "volume").fillna(0)
    oi = _num_col(df, "openInterest").fillna(0)
    for strike, vol, open_interest in zip(strikes, volume, oi):
        strike = _f(strike, 2)
        vol = _f(vol, 0)
        open_interest = _f(open_interest, 0)
        if strike is None or vol is None or open_interest is None:
            continue
        if vol > UNUSUAL_MIN_VOLUME and vol > open_interest:
            out.append({
                "side": side,
                "strike": strike,
                "volume": int(vol),
                "openInterest": int(open_interest),
                "expiry": expiry,
            })
    return out


def option_metrics(sym: str, spot=None) -> dict | None:
    """返回当日期权确认指标。任何数据源异常都不向上抛出。"""
    path = _cache_file(sym)
    cached = _load_cache(path)
    if cached is not None:
        return cached

    metrics = _empty_metrics(sym, spot)
    try:
        ticker = yf.Ticker(sym)
        metrics["spot"] = _f(spot) or _spot_from_ticker(ticker)

        expiries = list(ticker.options or [])
        today = dt.date.today()
        dated = [(exp, _parse_expiry(exp)) for exp in expiries]
        dated = [(exp, d) for exp, d in dated if d is not None and d >= today]
        if not dated:
            _save_cache(path, metrics)
            return metrics

        dated.sort(key=lambda item: item[1])
        front_expiry = dated[0][0]
        metrics["front_expiry"] = front_expiry
        near = [(exp, d) for exp, d in dated if (d - today).days <= NEAR_EXPIRY_DAYS]
        if not near:
            near = [dated[0]]

        call_oi = put_oi = call_vol = put_vol = 0.0
        call_walls: dict[float, float] = {}
        put_walls: dict[float, float] = {}
        unusual = []

        for exp, _ in near:
            try:
                chain = ticker.option_chain(exp)
                calls, puts = chain.calls, chain.puts
            except Exception:
                continue
            if exp == front_expiry:
                metrics["atm_iv"], metrics["iv_skew"] = _front_metrics(calls, puts, metrics["spot"])

            call_oi += _sum_col(calls, "openInterest")
            put_oi += _sum_col(puts, "openInterest")
            call_vol += _sum_col(calls, "volume")
            put_vol += _sum_col(puts, "volume")
            _add_wall(call_walls, calls, metrics["spot"], above=True)
            _add_wall(put_walls, puts, metrics["spot"], above=False)
            unusual.extend(_unusual_rows(calls, "call", exp))
            unusual.extend(_unusual_rows(puts, "put", exp))

        metrics["pc_oi"] = _ratio(put_oi, call_oi)
        metrics["pc_vol"] = _ratio(put_vol, call_vol)
        metrics["call_wall"] = _wall(call_walls)
        metrics["put_wall"] = _wall(put_walls)
        metrics["unusual"] = sorted(unusual, key=lambda x: x["volume"], reverse=True)[:3]
        _save_cache(path, metrics)
        return metrics
    except Exception:
        _save_cache(path, metrics)
        return metrics


def main() -> None:
    ap = argparse.ArgumentParser(description="Print yfinance option metrics")
    ap.add_argument("symbol", help="ticker, for example NVDA")
    args = ap.parse_args()
    print(json.dumps(option_metrics(args.symbol), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
