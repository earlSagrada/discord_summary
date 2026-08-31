"""把 signals.analyze() 产出的信号卡渲染成**中文推送文本**。

设计目标（按用户反馈）：
- **中文、说人话**："存在 priced-in 风险"而不是"Priced-in risk."
- **句子连贯**：用「——」「，」把因果串起来，最后落到一句结论（该不该追）。
- **短、可扫读**：数字压在一行用「｜」分隔，一张卡通常 3~6 行。
- **不留空行**：标题前、bullet 前都不空行（Discord 里更紧凑）。

信号是本地确定性算出来的，所以这里用静态模板，不调 AI（免费、稳定、可控）。
"""

from datetime import datetime, timezone

import config

_LIGHT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
_TIER = {"A": "A档", "B": "B档", "watch": "观察", "—": "无档"}


def _num(x, nd: int = 2) -> str:
    """数值 → 短字符串；None → '?'。"""
    if x is None:
        return "?"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _pct(x) -> str:
    """0.25 → '25%'。"""
    if x is None:
        return "?"
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(x)


# ───────────────────────── 触发原因 ─────────────────────────

def _signal_line(sig: dict, metrics: dict) -> str:
    """单条信号 → 一句中文。"""
    name = sig.get("name", "")
    high20 = _num(metrics.get("high_20"))
    vr = _num(metrics.get("vol_ratio"), 1)
    sma50 = _num(metrics.get("sma50"))

    if name == "关键位突破确认":
        return f"放量突破 20 日高 {high20}，量能 {vr} 倍（有量 = 有人真在买）"
    if name == "关键位突破(量不足)":
        return f"突破了 20 日高 {high20}，但量能只有 {vr} 倍——没量的突破常是假的"
    if name == "接近关键位":
        return f"逼近 20 日高 {high20}，还没突破（别提前进场）"
    if name == "reclaim 50日线":
        return f"收回 50 日线 {sma50} 上方"
    if name == "财报 beat":
        return "财报超预期，是新鲜催化"
    if name.startswith("财报 beat[已消化]"):
        return "财报虽超预期，但已是旧闻，只作背景参考"
    if name == "财报 miss":
        return "财报不及预期"
    if name.startswith("利空不跌"):
        return "利空后价格守住没创新低，疑似见底——但要多看几天才算数"
    return name


# ───────────────────────── priced-in 风险 ─────────────────────────

def _freshness_line(card: dict) -> str | None:
    """把过期标签串成一句连贯的「这信号可能已被市场消化」。"""
    fr = card.get("freshness") or {}
    flags = fr.get("flags") or []
    reasons = []
    if "extended" in flags and fr.get("extension_pct") is not None:
        reasons.append(f"现价已比进场位高出 {fr['extension_pct']}%")
    if "stale_breakout" in flags and fr.get("days_since_breakout") is not None:
        reasons.append(f"突破是 {fr['days_since_breakout']} 天前的事了")
    if "stale_chat" in flags and fr.get("mention_age_min") is not None:
        mins = fr["mention_age_min"]
        when = f"{mins // 60}小时" if mins >= 60 else f"{mins}分钟"
        reasons.append(f"群里已经{when}没人再提")
    if not reasons:
        return None
    return "⚠️ 存在 priced-in 风险：" + "，".join(reasons) + "——这波多半已被市场消化，现在追进去容易接盘"


# ───────────────────────── 期权确认 ─────────────────────────

def _has_options(opt: dict) -> bool:
    return any(opt.get(k) is not None for k in ("atm_iv", "pc_oi", "call_wall", "put_wall"))


def _options_lines(card: dict) -> list[str]:
    opt = card.get("options") or {}
    if not _has_options(opt):
        return []

    parts = []
    iv = opt.get("atm_iv")
    if iv is not None:
        judge = "偏贵" if float(iv) >= config.IV_HIGH else "不算贵"
        parts.append(f"IV {_pct(iv)}（{judge}）")
    if opt.get("put_wall") is not None:
        parts.append(f"下方 {_num(opt['put_wall'])} 有支撑")
    if opt.get("call_wall") is not None:
        parts.append(f"上方 {_num(opt['call_wall'])} 有阻力")
    lines = ["期权：" + " ｜ ".join(parts)] if parts else []

    if iv is not None and float(iv) >= config.IV_HIGH:
        lines.append("　IV 偏高说明期权本身很贵，市场已经在预期大波动（杠杆 ETP 天生如此，属正常）")

    un = opt.get("unusual") or []
    if un:
        u = un[0]
        side = "call" if u.get("side") == "call" else "put"
        lines.append(f"　异常成交：{_num(u.get('strike'))} 的 {side} 今天有 {u.get('volume')} 张新仓位，"
                     f"但免费数据看不出是买是卖，只能当线索")

    fr = card.get("freshness") or {}
    if "capped" in (fr.get("flags") or []):
        lines.append(f"　⚠️ 上方 {_num(fr.get('capped_call_wall'))} 的期权墙紧贴进场位，突破可能就卡在那")
    return lines


# ───────────────────────── 单卡 ─────────────────────────

def _price_vs_entry(price, entry) -> str:
    if price is None or entry in (None, 0):
        return ""
    gap = (price / entry - 1) * 100
    if gap < 0:
        return f"还差 {-gap:.1f}% 才触发"
    if gap <= 0.5:
        return "刚好在触发位附近"
    return f"已高出 {gap:.1f}%，属于追高"


def _card_block(card: dict) -> str:
    name = card.get("name") or ""
    head = f"{_LIGHT.get(card['light'], '')} **{card['ticker']}**"
    if name:
        head += f" {name}"
    head += f" · {_TIER.get(card.get('tier'), card.get('tier'))}"
    lines = [head]

    price, entry, stop, target = card.get("price"), card.get("entry"), card.get("stop"), card.get("target")
    if entry is not None and stop is not None:
        risk = (entry - stop) / entry * 100 if entry else None
        lines.append(f"现价 {_num(price)} ｜ 进场 {_num(entry)}（20日高）｜ "
                     f"止损 {_num(stop)}（-{_num(risk, 1)}%）｜ 目标 {_num(target)}")
        rel = _price_vs_entry(price, entry)
        if rel:
            lines.append(f"　{rel}；止损是这笔最多亏多少，目标约为风险的 2 倍")
    else:
        lines.append(f"现价 {_num(price)} ｜ 目前没有明确进场位（要自己定）")

    metrics = card.get("metrics", {})
    sigs = card.get("sig_objs", [])
    if sigs:
        lines.append("触发：" + "；".join(_signal_line(s, metrics) for s in sigs))

    fresh = _freshness_line(card)
    if fresh:
        lines.append(fresh)
    lines.extend(_options_lines(card))

    if not card.get("env_clear", True):
        reason = card.get("env_reason") or "今天环境不明朗"
        lines.append(f"⚠️ 今天别重仓：{reason}")
    return "\n".join(lines)


# ───────────────────────── 整段 ─────────────────────────

def format_cards(cards: list[dict], now: datetime | None = None) -> str:
    """信号卡列表 → 一段中文。🟢🟡 详列，🔴/无数据只用一句带过。"""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%H:%M UTC")

    detailed = [c for c in cards if not c.get("no_data") and c.get("light") in ("green", "yellow")]
    red = [c for c in cards if not c.get("no_data") and c.get("light") == "red"]
    no_data = [c for c in cards if c.get("no_data")]

    out = [f"**📊 交易信号** · {stamp}"]
    if detailed:
        detailed.sort(key=lambda c: 0 if c["light"] == "green" else 1)  # 绿灯在前
        out.extend(_card_block(c) for c in detailed)
    else:
        out.append("目前没有 🟢 或 🟡 信号。")

    if red:
        out.append(f"🔴 无信号（{len(red)}）：" + "、".join(c["ticker"] for c in red))
    if no_data:
        out.append("拿不到行情：" + "、".join(c["ticker"] for c in no_data))
    return "\n".join(out)
