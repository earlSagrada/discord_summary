"""变化检测：把「这一轮和上一轮/今天开盘相比，到底有什么不一样」算出来。

为什么需要它：cycle 每 15 分钟推一次，同一天里群里聊的往往还是那几件事。
如果每轮都让 AI 重新总结一遍全天讨论，用户看到的就是**同样的话被说 96 遍**。

做法分两层：
1. **确定性层（本模块）**：不调 AI，直接从 signals.db 里比出可验证的差异——
   今天第一次出现的票、灯色变了、价格走了多少、讨论升温还是降温。
   数字层面的"变化"必须是算出来的，不能让模型猜。
2. **叙事层（pulse.py）**：把「上几轮已经说过的话」喂给模型，要求它只讲增量。

关键约束：`snapshot()` 必须在本轮 `signals.analyze(save=True)` 写库**之前**调用，
否则本轮自己的记录会污染"上一轮"的基准。
"""

import json
from datetime import datetime, timezone

import config  # noqa: F401  (UTF-8 stdout + load .env)
import store

# 讨论增量达到这个次数才算"升温"，避免 1~2 次提及就报变化
HEAT_MIN = 3
# 价格变动超过这个百分比才值得单独说一句
MOVE_MIN_PCT = 1.0

_LIGHT_CN = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
_LIGHT_RANK = {"red": 0, "yellow": 1, "green": 2}


def _sig_names(raw) -> set[str]:
    """把存库的信号字符串列表还原成信号名集合：'关键位突破确认[A] 备注' → '关键位突破确认'。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return set()
    return {str(s).split("[")[0].strip() for s in (raw or []) if str(s).strip()}


def snapshot(now: datetime | None = None, conn=None) -> dict:
    """取"本轮写库之前"的今日状态：每票的首次/最近记录、上轮提及数、已推送过的简报。"""
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    own = conn is None
    conn = conn or store.connect()
    try:
        history: dict[str, list] = {}
        for ticker, ts, light, tier, price, sigs in store.day_signal_history(conn, day):
            history.setdefault(ticker, []).append(
                {"ts": ts, "light": light, "tier": tier, "price": price,
                 "signals": _sig_names(sigs)})
        return {
            "day": day,
            "history": history,
            "mentions": store.day_mention_counts(conn, day),
            "rounds": store.recent_rounds(conn, day, limit=3),
        }
    finally:
        if own:
            conn.close()


def compute(prior: dict, cards: list[dict], mentions: list[dict] | None = None) -> dict:
    """对比本轮结果与 prior 快照，产出结构化的变化清单。"""
    history = prior.get("history", {})
    prev_mentions = prior.get("mentions", {})
    now_mentions = {m["ticker"]: int(m.get("count") or 0) for m in (mentions or [])}

    fresh, lights, moves, faded = [], [], [], []
    seen_now = set()

    for c in cards:
        t = c["ticker"]
        seen_now.add(t)
        hist = history.get(t)
        if not hist:
            fresh.append({"ticker": t, "light": c.get("light"), "tier": c.get("tier"),
                          "price": c.get("price"),
                          "signals": sorted(_sig_names(c.get("signals")))})
            continue

        first, last = hist[0], hist[-1]

        if last.get("light") != c.get("light"):
            lights.append({
                "ticker": t, "from": last.get("light"), "to": c.get("light"),
                "up": _LIGHT_RANK.get(c.get("light"), 1) > _LIGHT_RANK.get(last.get("light"), 1),
                "added": sorted(_sig_names(c.get("signals")) - last.get("signals", set())),
                "dropped": sorted(last.get("signals", set()) - _sig_names(c.get("signals"))),
            })

        px, base, prev_px = c.get("price"), first.get("price"), last.get("price")
        if px and base:
            since_open = (px - base) / base * 100
            since_last = ((px - prev_px) / prev_px * 100) if prev_px else 0.0
            if abs(since_open) >= MOVE_MIN_PCT or abs(since_last) >= MOVE_MIN_PCT:
                moves.append({"ticker": t, "price": px,
                              "since_first": round(since_open, 1),
                              "since_last": round(since_last, 1)})

    heat = []
    for t, cnt in now_mentions.items():
        gain = cnt - prev_mentions.get(t, 0)
        if gain >= HEAT_MIN:
            heat.append({"ticker": t, "gain": gain, "total": cnt,
                         "first_time": t not in prev_mentions})
    heat.sort(key=lambda x: -x["gain"])

    # 之前今天讨论过、这轮完全没人提 → 话题冷掉了（只报之前较热的，避免噪音）
    for t, cnt in prev_mentions.items():
        if t not in now_mentions and cnt >= HEAT_MIN * 2:
            faded.append({"ticker": t, "total": cnt})

    return {
        "day": prior.get("day", ""),
        "round_no": len(prior.get("rounds", [])) + 1,
        "first_round": not prior.get("history") and not prior.get("rounds"),
        "new": fresh, "lights": lights, "moves": moves,
        "heat": heat[:5], "faded": faded[:3],
        "carried": sorted(seen_now - set(x["ticker"] for x in fresh)),
    }


def _light_txt(x) -> str:
    return _LIGHT_CN.get(x, "⚪")


def render(d: dict, max_lines: int = 6) -> str:
    """渲染成推送用的中文「变化」块。没有实质变化就返回空串（交给 cycle 决定怎么说）。

    同一只票只出一条：优先说评级变化 > 首次进榜 > 讨论升温 > 价格进展，
    否则一只票会在块里被念三遍——那正是我们要消灭的重复。
    """
    if d.get("first_round"):
        return ""
    lines: list[str] = []
    used: set[str] = set()

    def take(ticker: str) -> bool:
        if ticker in used:
            return False
        used.add(ticker)
        return True

    for x in d.get("lights", []):
        if not take(x["ticker"]):
            continue
        arrow = "升级" if x["up"] else "降级"
        why = ""
        if x["added"]:
            why = f"，新增{('、'.join(x['added']))[:40]}"
        elif x["dropped"]:
            why = f"，{('、'.join(x['dropped']))[:40]}失效"
        lines.append(f"- **{x['ticker']}** 评级{arrow}："
                     f"{_light_txt(x['from'])}→{_light_txt(x['to'])}{why}。")

    for x in d.get("new", [])[:3]:
        # 红灯/没信号的票不值得单独报"首次进榜"——那只是有人提了一嘴而已
        if not x["signals"] or x.get("light") == "red":
            continue
        if not take(x["ticker"]):
            continue
        sig = ('、'.join(x["signals"]))[:40]
        lines.append(f"- **{x['ticker']}** 今天首次进榜（{_light_txt(x['light'])}）：{sig}。")

    for x in d.get("heat", [])[:3]:
        if not take(x["ticker"]):
            continue
        tag = "刚被提起" if x["first_time"] else "讨论升温"
        lines.append(f"- **{x['ticker']}** {tag}：这段时间又被说了 {x['gain']} 次"
                     f"（今天共 {x['total']} 次）。")

    for x in d.get("moves", [])[:3]:
        if not take(x["ticker"]):
            continue
        lines.append(f"- **{x['ticker']}** 价格走到 {round(x['price'], 2)}："
                     f"较上轮 {x['since_last']:+.1f}%，今天累计 {x['since_first']:+.1f}%。")

    for x in d.get("faded", [])[:2]:
        if not take(x["ticker"]):
            continue
        lines.append(f"- **{x['ticker']}** 已经没人再提（今天共 {x['total']} 次），话题降温。")

    if not lines:
        return ""
    return "🔄 **和上一轮相比**\n" + "\n".join(lines[:max_lines])


def prior_context(prior: dict, max_chars: int = 1800) -> str:
    """把「今天已经推送过什么」整理成给模型看的上下文，让它别重复。"""
    rounds = prior.get("rounds") or []
    if not rounds:
        return "（今天还没推送过，这是第一条，可以正常完整介绍。）"
    parts = []
    for r in rounds:
        stamp = (r.get("ts") or "")[11:16]
        parts.append(f"【{stamp} UTC 那一轮已经说过】\n{r.get('brief', '').strip()}")
    text = "\n".join(parts)
    return text[-max_chars:]


def thread_hint(prior: dict) -> str:
    """今天已确立的主线（取最近一轮记的），供模型判断是延续还是转折。"""
    for r in reversed(prior.get("rounds") or []):
        if r.get("thread"):
            return r["thread"]
    return ""


def _demo() -> None:
    prior = snapshot()
    print(f"今天 {prior['day']}：已有 {len(prior['history'])} 只票的记录、"
          f"{len(prior['rounds'])} 轮推送、{len(prior['mentions'])} 条提及基准。")
    print("--- 已说过的内容 ---")
    print(prior_context(prior)[:800])


if __name__ == "__main__":
    _demo()
