"""宏观事件日历：自动判断"今天是不是大宏观日"（替代手动 --event-today）。

混合数据源（任一命中即算"有事件"）：
  1. 静态表 event_calendar.json —— 用户维护的 FOMC / 自定义大事件日期（最可靠的骨架）。
  2. FRED release dates —— 高影响经济数据发布（CPI/NFP/PPI/PCE/GDP/零售），best-effort。
  3. Finnhub / FMP 经济日历 —— 有免费额度就用，做交叉校验；受限就静默跳过。

设计意图（对齐方法论）：宏观只当 **regime filter**——"今天要不要下场 / 给多大仓"，
不押方向。命中 → cycle 把环境标为不 clear，信号卡里说明原因、建议 trade small。

结果按天缓存到 market_cache/events_<date>.json，避免 15min 轮询打爆免费额度。

自测：
    python src/events.py            # 打印今天的事件
    python src/events.py --date 2026-01-28
"""

import argparse
import datetime as dt
import json
import os

import requests

import config  # noqa: F401  (load .env + UTF-8)

CACHE = config.DATA_DIR / "market_cache"
CACHE.mkdir(parents=True, exist_ok=True)
FRED = "https://api.stlouisfed.org/fred"
FINNHUB = "https://finnhub.io/api/v1"
FMP = "https://financialmodelingprep.com/api/v3"

# 高影响 FRED release（按名称子串匹配）+ 已知 release_id 兜底
_HIGH_IMPACT_NAMES = [
    "Consumer Price Index",
    "Employment Situation",
    "Producer Price Index",
    "Personal Income and Outlays",
    "Gross Domestic Product",
    "Advance Monthly Sales for Retail",
]
_HIGH_IMPACT_IDS = {10, 50, 46, 54, 53}  # CPI / Employment / PPI / PCE / GDP


def _today() -> dt.date:
    return dt.date.today()


# ───────────────────────── 静态表 ─────────────────────────

def _load_static() -> list[dict]:
    path = config.EVENT_CALENDAR
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("events", []) if isinstance(data, dict) else data
    return [e for e in items if isinstance(e, dict) and e.get("date")]


def _static_today(day: dt.date) -> list[dict]:
    iso = day.isoformat()
    return [
        {"name": e.get("name", "event"), "source": "static", "impact": e.get("impact", "high")}
        for e in _load_static()
        if str(e.get("date"))[:10] == iso
    ]


# ───────────────────────── FRED ─────────────────────────

def _fred_today(day: dt.date) -> list[dict]:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return []
    iso = day.isoformat()
    try:
        r = requests.get(
            f"{FRED}/releases/dates",
            params={
                "api_key": key,
                "file_type": "json",
                "realtime_start": iso,
                "realtime_end": iso,
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
            },
            timeout=15,
        )
        if not r.ok:
            return []
        dates = r.json().get("release_dates", [])
    except (requests.RequestException, ValueError):
        return []

    out = []
    for d in dates:
        if str(d.get("date"))[:10] != iso:
            continue
        name = d.get("release_name", "") or ""
        rid = d.get("release_id")
        hit = rid in _HIGH_IMPACT_IDS or any(h.lower() in name.lower() for h in _HIGH_IMPACT_NAMES)
        if hit:
            out.append({"name": name or f"FRED release {rid}", "source": "fred", "impact": "high"})
    return out


# ───────────────────────── Finnhub / FMP（best-effort）─────────────────────────

def _finnhub_today(day: dt.date) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    iso = day.isoformat()
    try:
        r = requests.get(
            f"{FINNHUB}/calendar/economic",
            params={"from": iso, "to": iso, "token": key},
            timeout=15,
        )
        if not r.ok:  # 免费额度常返回 403 → 静默跳过
            return []
        cal = r.json().get("economicCalendar", []) if isinstance(r.json(), dict) else []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for c in cal:
        if str(c.get("country", "")).upper() not in ("US", "UNITED STATES"):
            continue
        if str(c.get("impact", "")).lower() in ("high", "3"):
            out.append({"name": c.get("event", "US event"), "source": "finnhub", "impact": "high"})
    return out


def _fmp_today(day: dt.date) -> list[dict]:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return []
    iso = day.isoformat()
    try:
        r = requests.get(
            f"{FMP}/economic_calendar",
            params={"from": iso, "to": iso, "apikey": key},
            timeout=15,
        )
        if not r.ok:
            return []
        cal = r.json() if isinstance(r.json(), list) else []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for c in cal:
        if str(c.get("country", "")).upper() not in ("US", "USD", "UNITED STATES"):
            continue
        if str(c.get("impact", "")).lower() == "high":
            out.append({"name": c.get("event", "US event"), "source": "fmp", "impact": "high"})
    return out


# ───────────────────────── 对外接口（带每日缓存）─────────────────────────

def _cache_path(day: dt.date):
    return CACHE / f"events_{day.isoformat()}.json"


def events_today(day: dt.date | None = None, force: bool = False) -> list[dict]:
    """今天所有命中的高影响宏观事件（去重）。按天缓存。"""
    day = day or _today()
    path = _cache_path(day)
    if not force and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    merged: list[dict] = []
    for fn in (_static_today, _fred_today, _finnhub_today, _fmp_today):
        try:
            merged.extend(fn(day))
        except Exception:  # 单个数据源坏掉不影响其它
            pass

    seen, deduped = set(), []
    for e in merged:
        k = e.get("name", "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            deduped.append(e)

    try:
        path.write_text(json.dumps(deduped, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return deduped


def is_event_today(day: dt.date | None = None) -> bool:
    return bool(events_today(day))


def event_names(day: dt.date | None = None) -> list[str]:
    return [e["name"] for e in events_today(day)]


def main() -> None:
    ap = argparse.ArgumentParser(description="宏观事件日历（自动 --event-today）")
    ap.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新拉取")
    args = ap.parse_args()
    day = dt.date.fromisoformat(args.date) if args.date else None
    evs = events_today(day, force=args.force)
    if not evs:
        print("今天没有命中高影响宏观事件（环境视为 clear）。")
        return
    print(f"今天有 {len(evs)} 个高影响宏观事件（环境不 clear）：")
    for e in evs:
        print(f"  - {e['name']}  [{e.get('source')}]")


if __name__ == "__main__":
    main()
