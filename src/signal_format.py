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


def _card_block(card: dict) -> str:
    ticker = card["ticker"]
    name = card.get("name") or ""
    head = f"{_LIGHT.get(card['light'], card['light'])} — {ticker}"
    if name:
        head += f" ({name})"
    head += f" · {_TIER.get(card.get('tier'), card.get('tier'))}"

    lines = [head]
    lines.append(
        f"  Price {_num(card.get('price'))}. Entry {_num(card.get('entry'))}. Stop {_num(card.get('stop'))}."
    )
    if not card.get("env_clear", True):
        lines.append("  Note: the market condition is not clear today.")
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
