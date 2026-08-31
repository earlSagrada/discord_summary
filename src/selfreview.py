"""每日自我复盘：不是复述今天发生了什么，而是回答"我们哪里分析得不够好"。

和 review.py（每周，看群和信号）的区别：
  - review.py  → 对**外**：群里谁有价值、信号引擎该怎么调参。
  - selfreview.py → 对**内**：我们自己写的简报和 Frank 的复盘范式差在哪，
    并把学到的方法写回 prompts/playbook.md，下一轮 pulse 立刻用上。

这就是闭环：pulse 产出 → 当天价格验证 → 复盘找差距 → playbook 变强 → pulse 变强。

喂给模型的六份材料：
  1. 今天群里的完整聊天       4. Frank 的 Substack 复盘（方法论标杆）
  2. 今天我们推过的所有简报    5. 当前 playbook
  3. 今天的信号 + 历史兑现情况  6. 当前 VIP 名单

产出：
  - trade_notes/reviews/<日期>-daily.md  完整报告
  - prompts/playbook.md                  自动追加「可以沉淀的方法」
  - prompts/vip_suggestions.md           VIP 名单建议（人工确认后才改正式名单）
  - Discord 推送 TL;DR

用法：
    python src/selfreview.py                 # 复盘今天
    python src/selfreview.py --day 20260827  # 复盘指定某天
    python src/selfreview.py --no-post --no-apply
    python src/selfreview.py --no-api        # 只看喂进去什么（不花钱）
"""

import argparse
import difflib
import json
import os
import re
from datetime import datetime, timedelta, timezone

import config
import prompts
import pulse
import store

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 8000
REVIEWS_DIR = config.PROJECT_ROOT / "trade_notes" / "reviews"
MAX_CHAT_CHARS = 60000
PLAYBOOK_MAX = 20
# 两条方法的相似度超过这个值就当成重复。实测：同义条目 0.59，
# 真正不同的条目最高才 0.05，中间空档很大，取 0.45 很安全。
PLAYBOOK_SIMILAR = 0.45


# ───────────────────────── 收集材料 ─────────────────────────

def day_chat(day: str) -> str:
    """某天（YYYYMMDD）各频道的 enriched 聊天记录。"""
    base = config.CHATS_DIR / day
    if not base.exists():
        return ""
    chunks = []
    paths = sorted(base.glob("*/merged.enriched.txt"))
    flat = base / "merged.enriched.txt"
    if flat.exists():
        paths.append(flat)
    for p in paths:
        try:
            chunks.append(f"### 频道 {p.parent.name}\n{p.read_text(encoding='utf-8')}")
        except OSError:
            pass
    text = "\n\n".join(chunks)
    # 太长就掐掉开头，保留当天后半段（收盘附近信息量最大）
    return text[-MAX_CHAT_CHARS:]


def day_briefs(conn, iso_day: str) -> str:
    """今天我们自己推出去的所有简报，按时间排列——这是被审视的对象。"""
    rounds = store.recent_rounds(conn, iso_day, limit=50)
    if not rounds:
        return "（今天没有推送过简报。）"
    parts = []
    for r in rounds:
        stamp = (r.get("ts") or "")[11:16]
        parts.append(f"【{stamp} UTC · 涉及 {'、'.join(r['tickers'][:8]) or '无'}】\n"
                     f"{r.get('brief', '').strip()}")
    return "\n\n".join(parts)


def day_signals(conn, iso_day: str) -> str:
    """今天发过的信号，按票去重（同一只票 15 分钟记一次，不去重会刷屏）。"""
    rows = store.day_signal_history(conn, iso_day)
    if not rows:
        return "（今天没有产生信号。）"
    latest: dict[str, tuple] = {}
    first: dict[str, tuple] = {}
    for ticker, ts, light, tier, price, sigs in rows:
        first.setdefault(ticker, (ts, price))
        latest[ticker] = (ts, light, tier, price, sigs)
    lines = []
    for ticker, (ts, light, tier, price, sigs) in latest.items():
        try:
            names = "、".join(json.loads(sigs or "[]")) or "无"
        except (TypeError, ValueError):
            names = str(sigs)
        open_px = first[ticker][1]
        move = f"，当天从 {open_px} 走到 {price}" if (open_px and price) else ""
        lines.append(f"- {ticker}：{light} {tier}档{move}；信号：{names}")
    return "\n".join(lines)


def outcomes_text() -> str:
    """历史信号的兑现统计（去重后的真实胜率）。"""
    try:
        import backtest
        return "### 历史信号兑现情况\n" + backtest.report_text()
    except Exception as e:
        return f"（兑现统计不可用：{type(e).__name__}）"


def substack_text(limit: int = 1) -> str:
    try:
        import substack
        body, used = substack.load_recent(limit)
        if not body:
            return "（还没有可用的 Substack 复盘 PDF。）"
        return f"（来源：{', '.join(used)}）\n{body}"
    except Exception as e:
        return f"（Substack 复盘读取失败：{type(e).__name__}: {e}）"


# ───────────────────────── 解析产出 ─────────────────────────

def section(md: str, heading_key: str) -> str:
    """抽出某个 '## 标题' 到下一个 '## ' 之间的内容（不含标题行）。"""
    out, capture = [], False
    for line in md.splitlines():
        if line.strip().startswith("## "):
            if capture:
                break
            capture = heading_key in line
            continue
        if capture:
            out.append(line)
    return "\n".join(out).strip()


def _bullets(text: str) -> list[str]:
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- ") and len(line) > 6 and "没有新方法" not in line:
            items.append(line)
    return items


def _norm(s: str) -> str:
    return re.sub(r"\W", "", s)


def _is_dup(item: str, existing: list[str]) -> bool:
    """近义去重：模型很容易把同一条方法换个说法再写一遍。

    精确匹配挡不住"分批止盈的具体操作"和"分批止盈+保留仓位+准备回补"这种，
    它们会一起挤占 pulse 只取前 8 条的名额，稀释真正有用的条目。
    """
    a = _norm(item)
    for old in existing:
        if difflib.SequenceMatcher(None, a, _norm(old)).ratio() >= PLAYBOOK_SIMILAR:
            return True
    return False


def update_playbook(report: str, day: str) -> int:
    """把「可以沉淀的方法」写回 playbook，新的放最前面（pulse 只取前 8 条）。

    条目上限 PLAYBOOK_MAX；被挤出去的落到「## 已淘汰」，方便日后回看"试过什么"。
    """
    new_items = _bullets(section(report, "可以沉淀的方法"))
    if not new_items:
        return 0
    path = prompts.PROMPTS_DIR / "playbook.md"
    text = path.read_text(encoding="utf-8")
    if pulse.PB_BEGIN not in text or pulse.PB_END not in text:
        return 0
    head, rest = text.split(pulse.PB_BEGIN, 1)
    body, tail = rest.split(pulse.PB_END, 1)

    old = pulse.playbook_items(body)
    fresh: list[str] = []
    for item in new_items:
        if _is_dup(item, old + fresh):
            continue
        fresh.append(item)
    if not fresh:
        return 0

    merged = fresh + old
    kept, dropped = merged[:PLAYBOOK_MAX], merged[PLAYBOOK_MAX:]
    if dropped:
        retired = f"<!-- {day} 挤出 -->\n" + "\n".join(dropped)
        tail = (tail.replace("（还没有）", retired, 1) if "（还没有）" in tail
                else tail.rstrip() + "\n\n" + retired + "\n")
    path.write_text(
        head + pulse.PB_BEGIN + "\n" + "\n".join(kept) + "\n" + pulse.PB_END + tail,
        encoding="utf-8")
    return len(fresh)


def dedupe_playbook() -> int:
    """清理已有 playbook 里的近义重复（保留靠前那条）。返回删掉几条。"""
    path = prompts.PROMPTS_DIR / "playbook.md"
    text = path.read_text(encoding="utf-8")
    if pulse.PB_BEGIN not in text or pulse.PB_END not in text:
        return 0
    head, rest = text.split(pulse.PB_BEGIN, 1)
    body, tail = rest.split(pulse.PB_END, 1)
    items = pulse.playbook_items(body)
    kept: list[str] = []
    for item in items:
        if not _is_dup(item, kept):
            kept.append(item)
    removed = len(items) - len(kept)
    if removed:
        path.write_text(
            head + pulse.PB_BEGIN + "\n" + "\n".join(kept) + "\n" + pulse.PB_END + tail,
            encoding="utf-8")
    return removed


def write_vip_suggestions(report: str, day: str) -> bool:
    sec = section(report, "VIP 名单建议")
    if not sec or "维持现状" in sec:
        return False
    path = prompts.PROMPTS_DIR / "vip_suggestions.md"
    path.write_text(
        f"# 由 selfreview.py 生成（{day}），人工确认后再改 vip_speakers.txt\n\n{sec}\n",
        encoding="utf-8")
    return True


# ───────────────────────── 主流程 ─────────────────────────

def build_prompt(day: str, iso_day: str, conn) -> tuple[str, str]:
    """返回 (prompt, 聊天记录)。聊天为空时上层直接跳过。"""
    chat = day_chat(day)
    tpl = prompts.load("daily_review.md")
    filled = tpl.format(
        day=iso_day,
        chat=chat or "（这一天没有聊天记录。）",
        briefs=day_briefs(conn, iso_day),
        signals=day_signals(conn, iso_day),
        outcomes=outcomes_text(),
        substack=substack_text(),
        playbook=pulse.load_playbook(limit=PLAYBOOK_MAX),
        vips="\n".join(f"- {v}" for v in pulse.load_vips()) or "- （还没设置）",
    )
    return filled, chat


def run(day: str | None = None, model: str = DEFAULT_MODEL, post: bool = True,
        apply_: bool = True, no_api: bool = False) -> str:
    now = datetime.now(timezone.utc)
    day = day or now.strftime("%Y%m%d")
    iso_day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"

    conn = store.connect()
    try:
        prompt, chat = build_prompt(day, iso_day, conn)
    finally:
        conn.close()

    if not chat.strip():
        print(f"{iso_day} 没有聊天记录，跳过自我复盘。")
        return ""

    if no_api:
        print(prompt[:4000] + f"\n…（prompt 共 {len(prompt)} 字）")
        return ""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("需要设置 ANTHROPIC_API_KEY 才能生成复盘")

    from anthropic import Anthropic
    client = Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    report = "\n".join(getattr(b, "text", "") for b in (resp.content or [])).strip()

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEWS_DIR / f"{day}-daily.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"每日复盘已写入 {out_path}")

    if apply_:
        n = update_playbook(report, iso_day)
        print(f"playbook 新增 {n} 条方法" if n else "playbook 无新增")
        if write_vip_suggestions(report, iso_day):
            print("VIP 名单建议已更新 prompts/vip_suggestions.md")

    if post:
        tldr = section(report, "TL;DR") or report[:1200]
        msg = (f"🪞 **每日自我复盘** · {iso_day}\n{pulse.tighten(tldr)}\n"
               f"_完整报告见 trade_notes/reviews/{out_path.name}_")
        try:
            import discord_post
            discord_post.send(msg)
            print("TL;DR 已推送到 Discord")
        except Exception as e:
            print(f"推送失败：{type(e).__name__}: {e}")

    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="每日自我复盘（学方法 + 找自己的短板）")
    ap.add_argument("--day", default=None, help="复盘哪一天 YYYYMMDD（默认今天 UTC）")
    ap.add_argument("--yesterday", action="store_true", help="复盘昨天（收盘后跑更合适）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-post", action="store_true", help="不推送 Discord")
    ap.add_argument("--no-apply", action="store_true", help="不改 playbook / VIP 建议")
    ap.add_argument("--no-api", action="store_true", help="只打印 prompt（离线自测）")
    ap.add_argument("--dedupe", action="store_true",
                    help="只清理 playbook 里的近义重复条目，不跑复盘")
    args = ap.parse_args()

    if args.dedupe:
        n = dedupe_playbook()
        print(f"playbook 清掉 {n} 条近义重复" if n else "playbook 没有近义重复")
        return

    day = args.day
    if args.yesterday and not day:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    run(day=day, model=args.model, post=not args.no_post,
        apply_=not args.no_apply, no_api=args.no_api)


if __name__ == "__main__":
    main()
