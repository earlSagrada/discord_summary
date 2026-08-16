"""把 signals.analyze() 产出的信号卡渲染成 ASD-STE100 简化技术英语文本。

信号是本地确定性算出来的，所以这里用**静态模板**把中文信号名翻成 STE 英语，
不调 AI（免费、稳定、可控）。cycle.py 把这段拼到脉搏简报后面一起推送到 Discord。

规则遵循 STE 精神：短句、主动语态、现在时、常用词、专有名词/代码保持原样。
"""

from datetime import datetime, timezone

# 灯 -> STE 标签
_LIGHT = {
    "green": "🟢 Good setup",
    "yellow": "🟡 Check it",
    "red": "🔴 No signal",
}

# tier -> STE 词
_TIER = {"A": "tier A", "B": "tier B", "watch": "watch", "—": "no tier"}


def _num(x) -> str:
    """把数值格式化成短字符串；None -> '?'。"""
    if x is None:
        return "?"
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


def _signal_line(sig: dict, metrics: dict) -> str:
    """单条信号 -> 一句 STE 英语。按信号名走静态模板。"""
    name = sig.get("name", "")
    high20 = _num(metrics.get("high_20"))
    vr = _num(metrics.get("vol_ratio"))
    sma50 = _num(metrics.get("sma50"))

    if name == "关键位突破确认":
        return f"Breakout, confirmed. The price is above the 20-day high {high20}. The volume is {vr} times the usual volume."
    if name == "关键位突破(量不足)":
        return f"Breakout, but the volume is low. The price is above the 20-day high {high20}. The volume is {vr} times the usual volume."
    if name == "接近关键位":
        return f"The price is near the 20-day high {high20}."
    if name == "reclaim 50日线":
        return f"The price moves back above the 50-day average {sma50}."
    if name == "财报 beat":
        return "The company reports earnings that are better than the estimate."
    if name == "财报 miss":
        return "The company reports earnings that are worse than the estimate."
    if name.startswith("利空不跌"):
        return "There is bad news, but the price holds. The price does not make a new low. This needs more days to confirm."
    # 未知信号名的兜底（正常不会走到）
    return f"Signal: {name}."


_FLAG_TEXT = {
    "extended": "The price is already {extension_pct}% above the entry.",
    "stale_breakout": "The breakout is {days_since_breakout} days old.",
    "earnings_consumed": "The earnings news is {days_since_earnings} days old.",
    "stale_chat": "The group did not talk about it for {mention_age_min} minutes.",
}


def _price_vs_entry(price, entry) -> str:
    """现价与入场位的关系，一句 STE。"""
    if price is None or entry in (None, 0):
        return ""
    gap = (price / entry - 1) * 100
    if gap >= 0:
        near = "You are still near the trigger." if gap <= 0.5 else "You are late. Wait for a pullback."
        return f"The price is {gap:.1f}% above the entry. {near}"
    return f"The price is {-gap:.1f}% below the entry. The signal is not triggered yet."


def _freshness_line(card: dict) -> str | None:
    """把过期标签拼成一句"这信号可能已被 price in"的告警。"""
    fr = card.get("freshness") or {}
    flags = fr.get("flags") or []
    if not flags:
        return None
    parts = []
    for f in flags:
        tmpl = _FLAG_TEXT.get(f)
        if tmpl:
            try:
                parts.append(tmpl.format(**fr))
            except (KeyError, ValueError):
                pass
    if not parts:
        return None
    return "  ⚠ Priced-in risk. " + " ".join(parts) + " Maybe the market already knows this."


def _pct(x) -> str:
    if x is None:
        return "?"
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(x)


def _has_options(opt: dict) -> bool:
    keys = ("atm_iv", "iv_skew", "pc_oi", "pc_vol", "call_wall", "put_wall", "front_expiry")
    return bool(opt) and (any(opt.get(k) is not None for k in keys) or bool(opt.get("unusual")))


def _options_lines(card: dict) -> list[str]:
    """期权只输出确认/风险上下文，不输出方向判断。"""
    opt = card.get("options") or {}
    if not _has_options(opt):
        return []

    parts = []
    if opt.get("atm_iv") is not None:
        parts.append(f"IV is {_pct(opt.get('atm_iv'))}.")
    if opt.get("pc_oi") is not None:
        parts.append(f"P/C OI is {_num(opt.get('pc_oi'))}.")
    if opt.get("put_wall") is not None:
        parts.append(f"Support (put wall) is near {_num(opt.get('put_wall'))}.")
    if opt.get("call_wall") is not None:
        parts.append(f"Resistance (call wall) is near {_num(opt.get('call_wall'))}.")
    lines = ["  Options: " + " ".join(parts)] if parts else []

    try:
        import config
        high_iv = opt.get("atm_iv") is not None and float(opt.get("atm_iv")) >= config.IV_HIGH
    except (ImportError, TypeError, ValueError):
        high_iv = False
    if high_iv:
        lines.append("  IV is high (options are expensive). The market maybe expects a big move already.")

    for u in (opt.get("unusual") or [])[:3]:
        strike = _num(u.get("strike"))
        side = u.get("side") or "option"
        vol = u.get("volume")
        lines.append(f"  Unusual option volume at {strike} {side} ({vol} lots). "
                     "This is new. The direction is not sure.")

    flags = (card.get("freshness") or {}).get("flags") or []
    if "capped" in flags:
        lines.append("  Note: a big call wall is just above the entry. The breakout may stop there.")
    return lines


def _card_block(card: dict) -> str:
    ticker = card["ticker"]
    name = card.get("name") or ""
    head = f"{_LIGHT.get(card['light'], card['light'])} — {ticker}"
    if name:
        head += f" ({name})"
    head += f" · {_TIER.get(card.get('tier'), card.get('tier'))}"

    lines = [head]
    price, entry, stop, target = card.get("price"), card.get("entry"), card.get("stop"), card.get("target")
    if entry is not None and stop is not None:
        risk = (entry - stop) / entry * 100 if entry else None
        lines.append(f"  Entry {_num(entry)} = the 20-day high (the breakout level). "
                     f"Stop {_num(stop)} = {_num(risk)}% below entry (your maximum risk).")
        rel = _price_vs_entry(price, entry)
        lines.append(f"  Price {_num(price)}. {rel}".rstrip())
        if target is not None:
            lines.append(f"  Target near {_num(target)} (about 2 times the risk).")
    else:
        lines.append(f"  Price {_num(price)}. There is no clear entry level now.")

    fresh = _freshness_line(card)
    if fresh:
        lines.append(fresh)
    if not card.get("env_clear", True):
        reason = card.get("env_reason") or "the market condition is not clear today"
        lines.append(f"  Note: do not trade big today. Reason: {reason}.")
    lines.extend(_options_lines(card))

    metrics = card.get("metrics", {})
    for sig in card.get("sig_objs", []):
        lines.append("  - " + _signal_line(sig, metrics))
    if not card.get("sig_objs"):
        lines.append("  - No signal now.")
    return "\n".join(lines)


def format_cards(cards: list[dict], now: datetime | None = None) -> str:
    """把信号卡列表渲染成一段 STE 英语。🟢🟡 详列，🔴/无数据只统计一句。"""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%H:%M UTC")

    detailed = [c for c in cards if not c.get("no_data") and c.get("light") in ("green", "yellow")]
    red = [c for c in cards if not c.get("no_data") and c.get("light") == "red"]
    no_data = [c for c in cards if c.get("no_data")]

    out = [f"**Trade signals** (update at {stamp})"]

    if detailed:
        # 绿灯在前，黄灯在后
        detailed.sort(key=lambda c: 0 if c["light"] == "green" else 1)
        for c in detailed:
            out.append("")
            out.append(_card_block(c))
    else:
        out.append("")
        out.append("No 🟢 or 🟡 signal now.")

    if red:
        names = ", ".join(c["ticker"] for c in red)
        word = "ticker has" if len(red) == 1 else "tickers have"
        out.append("")
        out.append(f"🔴 {len(red)} {word} no signal now: {names}.")
    if no_data:
        names = ", ".join(c["ticker"] for c in no_data)
        out.append(f"No market data for: {names}.")

    return "\n".join(out)
