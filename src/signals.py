"""信号打分引擎（手动触发）。

用法:
    # 从群聊记录抽重点标的(ETP/ETF)并打分
    python src/signals.py data/chats_by_date/20260729/merged.enriched.txt

    # 直接指定 watchlist
    python src/signals.py --watchlist SOXL,SOXS,QQQ

    # 把 FOMC/大数据当天标成"环境不clear"
    python src/signals.py --watchlist SOXL --event-today

只做设计文档 v0 的 3 类信号（关键位突破 / 财报催化 / 利空不跌）+ checklist 前 4 条，
输出信号卡（绿/黄/红灯）并写入 data/signals.db。信号规则是 v0 粗版，重在跑通闭环 + 积累"信号→结果"。
"""

import argparse
import sys
from pathlib import Path

import config  # noqa: F401  (load .env)
import extract
import levels as L
import market
import store
import tickers as T


# ────────────────────────── 信号检测 ──────────────────────────

def detect(lv: dict, earnings: dict | None) -> list[dict]:
    out: list[dict] = []
    last, high20, vr = lv.get("last"), lv.get("high_20"), lv.get("vol_ratio")

    if last and high20:
        if last > high20:
            note = f"收{last} 上破20日高{high20}"
            if vr and vr >= 1.2:
                out.append({"name": "关键位突破确认", "tier": "A", "note": note + f"，量{vr}×"})
            else:
                out.append({"name": "关键位突破(量不足)", "tier": "B",
                            "note": note + (f"，量{vr}×" if vr else "，无量能数据")})
        elif last >= high20 * 0.99:
            out.append({"name": "接近关键位", "tier": "watch",
                        "note": f"距20日高{high20} {round((high20 / last - 1) * 100, 2)}%"})

    sma50, prev_close = lv.get("sma50"), lv.get("prev_close")
    if last and sma50 and prev_close and prev_close < sma50 <= last:
        out.append({"name": "reclaim 50日线", "tier": "B", "note": f"收回{sma50}上方"})

    if earnings and earnings.get("surprisePercent") is not None:
        sp = earnings["surprisePercent"]
        detail = f"actual {earnings.get('actual')} vs est {earnings.get('estimate')}"
        if sp > 0:
            out.append({"name": "财报 beat", "tier": "A", "note": f"{detail}（+{round(sp, 1)}%）"})
        elif sp < 0:
            out.append({"name": "财报 miss", "tier": "C", "note": f"{detail}（{round(sp, 1)}%）"})

    return out


def detect_holddown(daily, lev: int = 1) -> dict | None:
    """利空不跌代理：近5日内出现单日暴跌(≤阈值)，但随后收盘守住其低点且未创新低。

    没有新闻标签，纯价格代理 → 一律 B 档、标"需多日确认"。
    阈值按杠杆倍数放大（-4% × lev），避免 3x/2x ETP 常规波动误触发。
    """
    if daily is None or len(daily) < 3:
        return None
    thr = -4.0 * max(1, lev)
    close = daily["Close"]
    low = daily["Low"]
    rets = close.pct_change() * 100
    tail = range(max(1, len(close) - 5), len(close) - 1)
    for i in tail:
        if rets.iloc[i] <= thr:
            drop_low = low.iloc[i]
            after = close.iloc[i + 1:]
            if not after.empty and (after >= drop_low).all() and close.iloc[-1] >= close.iloc[i]:
                return {"name": "利空不跌[未确认]", "tier": "B",
                        "note": f"{round(rets.iloc[i], 1)}% 急跌后守住{round(float(drop_low), 2)}，需多日确认，勿当天all in"}
    return None


# ────────────────────────── checklist + 灯 ──────────────────────────

def score(sigs: list[dict], env_clear: bool, entry, stop) -> tuple[dict, str, str]:
    tiers = {s["tier"] for s in sigs}
    is_A = "A" in tiers
    checklist = {
        "1_环境clear": env_clear,
        "2_A档信号": is_A,
        "3_入场点位明确": entry is not None,
        "4_止损位明确": stop is not None,
    }
    if is_A and env_clear and stop is not None:
        light = "green"
    elif not sigs or tiers <= {"C"}:
        light = "red"
    else:
        light = "yellow"
    tier = "A" if is_A else "B" if "B" in tiers else "watch" if "watch" in tiers else "—"
    return checklist, light, tier


def build_card(ticker: str, sym: str, lv: dict, sigs: list[dict], env_clear: bool) -> dict:
    last, high20 = lv.get("last"), lv.get("high_20")
    has_break = any("关键位" in s["name"] for s in sigs)
    entry = high20 if (has_break and high20) else None
    stop = round(entry * 0.97, 2) if entry else None
    checklist, light, tier = score(sigs, env_clear, entry, stop)
    meta = T.UNIVERSE.get(ticker, {})
    return {
        "ticker": ticker,
        "type": meta.get("type", "unknown"),
        "name": meta.get("name", ""),
        "price": last,
        "tier": tier,
        "light": light,
        "signals": [f"{s['name']}[{s['tier']}] {s['note']}" for s in sigs],
        # 结构化原始信号 + 关键位数值，供 STE 渲染 / 下游消费（不影响原有打印）
        "sig_objs": [dict(s) for s in sigs],
        "metrics": {
            "high_20": lv.get("high_20"),
            "low_20": lv.get("low_20"),
            "vol_ratio": lv.get("vol_ratio"),
            "ema9": lv.get("ema9"),
            "ema21": lv.get("ema21"),
            "sma50": lv.get("sma50"),
            "vwap": lv.get("vwap"),
            "chg_pct": lv.get("chg_pct"),
        },
        "env_clear": env_clear,
        "entry": entry,
        "stop": stop,
        "checklist": checklist,
        "notes": f"ema9={lv.get('ema9')} ema21={lv.get('ema21')} sma50={lv.get('sma50')} "
                 f"vwap={lv.get('vwap')} vol_ratio={lv.get('vol_ratio')}",
    }


_LIGHT = {"green": "🟢绿灯", "yellow": "🟡黄灯", "red": "🔴红灯"}


def print_card(c: dict) -> None:
    print(f"\n── {c['ticker']} ({c['type']} · {c['name']}) ──  {_LIGHT.get(c['light'], c['light'])}  档位:{c['tier']}")
    print(f"   现价 {c['price']}  建议入场 {c['entry']}  建议止损 {c['stop']}")
    if c["signals"]:
        for s in c["signals"]:
            print(f"   • {s}")
    else:
        print("   • 无触发信号")
    ck = c["checklist"]
    print("   checklist: " + "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in ck.items()))
    print(f"   {c['notes']}")


# ────────────────────────── 标的解析 ──────────────────────────

def resolve_symbol(raw: str) -> str:
    """watchlist 里的输入 -> UNIVERSE 规范 ticker（yfinance symbol）。"""
    raw = raw.strip()
    if raw in T.UNIVERSE:
        return raw
    latin, cjk = T.latin_alias_index(), T.cjk_alias_index()
    if raw.lower() in latin:
        return latin[raw.lower()]
    if raw in cjk:
        return cjk[raw]
    return raw.upper()


def resolve_from_text(text: str, all_: bool = False) -> tuple[list[str], list[dict], dict]:
    """从聊天文本抽标的 -> (syms, mentions, unknown)。all_=False 只留重点 ETP/ETF。"""
    mentions, unknown = extract.extract_mentions(text)
    pool = mentions if all_ else [m for m in mentions if m["focus"]]
    syms = [m["ticker"] for m in pool]
    return syms, mentions, unknown


# ────────────────────────── 打分（可复用） ──────────────────────────

def score_symbol(sym: str, event_today: bool = False) -> dict | None:
    """给单个标的拉数据、算位、打分，返回信号卡；拿不到行情返回 None。"""
    daily = market.get_daily(sym)
    if daily is None or daily.empty:
        return None
    intraday = market.get_intraday(sym)
    lv = L.compute_levels(daily, intraday)

    meta = T.UNIVERSE.get(sym, {})
    earnings = market.get_last_earnings(sym) if meta.get("type") == "stock" else None
    env_clear = not event_today
    if meta.get("type") == "stock" and market.upcoming_earnings_within(sym, 2):
        env_clear = False

    sigs = detect(lv, earnings)
    hd = detect_holddown(daily, meta.get("lev", 1))
    if hd:
        sigs.append(hd)
    return build_card(sym, sym, lv, sigs, env_clear)


def analyze(
    syms: list[str],
    *,
    event_today: bool = False,
    save: bool = True,
    source_label: str = "",
    mentions: list[dict] | None = None,
) -> tuple[list[dict], int | None]:
    """给一组标的打分，返回 (cards, run_id)。cards 里拿不到行情的标 no_data。

    cycle.py / signals CLI 共用这一条。save=True 时同时写入 signals.db。
    """
    mentions = mentions or []
    conn = store.connect() if save else None
    run_id = store.new_run(conn, source_label) if conn else None
    if conn:
        for m in mentions:
            store.add_mention(conn, run_id, m)

    cards: list[dict] = []
    for sym in syms:
        card = score_symbol(sym, event_today)
        if card is None:
            cards.append({"ticker": sym, "no_data": True})
            continue
        cards.append(card)
        if conn:
            store.add_signal(conn, run_id, card)

    if conn:
        conn.close()
    return cards, run_id


# ────────────────────────── main ──────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="信号打分引擎 v0")
    ap.add_argument("source", nargs="?", type=Path, help="merged.enriched.txt（抽标的用）")
    ap.add_argument("--watchlist", help="逗号分隔，直接指定标的，绕过抽取")
    ap.add_argument("--all", action="store_true", help="连非重点(个股/期货)一起打分")
    ap.add_argument("--event-today", action="store_true", help="FOMC/大数据当天：环境标为不clear")
    ap.add_argument("--limit", type=int, default=12, help="最多打分多少个标的")
    ap.add_argument("--no-save", action="store_true", help="不写入 signals.db")
    args = ap.parse_args()

    mentions, unknown = [], {}
    if args.watchlist:
        syms = [resolve_symbol(x) for x in args.watchlist.split(",") if x.strip()]
        source_label = "watchlist:" + args.watchlist
    elif args.source:
        text = args.source.read_text(encoding="utf-8")
        syms, mentions, unknown = resolve_from_text(text, args.all)
        source_label = str(args.source)
    else:
        ap.error("需要提供 merged.enriched.txt 或 --watchlist")

    syms = syms[: args.limit]
    if mentions:
        print("抽到标的：", ", ".join(f"{m['ticker']}×{m['count']}" for m in mentions))
        skipped = [m for m in mentions if not m["focus"]]
        if skipped and not args.all:
            preview = ", ".join(m["ticker"] for m in skipped[:10])
            print(f"（只打分重点 ETP/ETF；跳过 {len(skipped)} 个个股/期货，加 --all 可纳入：{preview}）")
        if unknown:
            print("未收录 cashtag（可考虑加入 tickers.py）：", unknown)
    if not syms:
        print("没有可打分的标的。")
        return

    cards, run_id = analyze(
        syms,
        event_today=args.event_today,
        save=not args.no_save,
        source_label=source_label,
        mentions=mentions,
    )
    for card in cards:
        if card.get("no_data"):
            print(f"\n── {card['ticker']} ──  ⚠️ 拿不到行情，跳过")
        else:
            print_card(card)

    if run_id is not None:
        print(f"\n已写入 {store.DB_PATH}（run #{run_id}）")


if __name__ == "__main__":
    main()
