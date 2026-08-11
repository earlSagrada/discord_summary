"""脉搏简报：从 merged.enriched.txt 里切出"最近 N 分钟"的新消息，
交给便宜的 Claude（haiku）生成一段 ASD-STE100 简化技术英语的"现在在聊什么"。

- 时间切片纯本地做（读 enriched 文本里的日期头 + HH:MM 时间行，均为 UTC）。
- 窗口内没有消息就返回空串 -> cycle.py 据此跳过 AI 调用与推送（省钱、不刷屏）。
- prompt 放在 prompts/pulse_summary.md，改措辞不用动代码。

用法（自测）:
    python src/pulse.py data/chats_by_date/20260803/frank/merged.enriched.txt --minutes 60 --anchor last
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config  # noqa: F401  (UTF-8 stdout + load .env)
import prompts

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500

_DAY_HDR = re.compile(r"^=====\s*(\d{4}-\d{2}-\d{2})\s*=====")
_TIME_LINE = re.compile(r"^(\d{2}):(\d{2})\s+\S")
_MANIFEST_MARK = "图片清单"


# ───────────────────────── 时间切片 ─────────────────────────

def _blocks(enriched_text: str) -> list[tuple[datetime, list[str]]]:
    """把 enriched 文本拆成 (UTC时间, [该条消息的所有行]) 的块。"""
    day: str | None = None
    cur: tuple[datetime, list[str]] | None = None
    out: list[tuple[datetime, list[str]]] = []
    for line in enriched_text.splitlines():
        if line.startswith("=====") and _MANIFEST_MARK in line:
            break  # 图片清单之后不是聊天正文
        m = _DAY_HDR.match(line)
        if m:
            day = m.group(1)
            continue
        t = _TIME_LINE.match(line)
        if t and day:
            if cur:
                out.append(cur)
            dt = datetime.strptime(f"{day} {t.group(1)}:{t.group(2)}",
                                   "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            cur = (dt, [line])
        elif cur:
            cur[1].append(line)
    if cur:
        out.append(cur)
    return out


def _render(blocks: list[tuple[datetime, list[str]]]) -> str:
    lines: list[str] = []
    last_day: str | None = None
    for dt, blk in blocks:
        day = dt.strftime("%Y-%m-%d")
        if day != last_day:
            lines.append(f"===== {day} =====")
            last_day = day
        lines.extend(blk)
    return "\n".join(lines)


def last_ts(enriched_text: str) -> datetime | None:
    blocks = _blocks(enriched_text)
    return blocks[-1][0] if blocks else None


def combine_recent(
    enriched_texts: list[str],
    minutes: int = 45,
    now: datetime | None = None,
) -> tuple[str, int]:
    """把多份 enriched 文本（各频道）里最近 minutes 分钟的消息合并、按时间排序。

    返回 (合并文本, 消息条数)。now 默认取真实 UTC 现在；离线测试可传入锚点时间。
    """
    all_blocks: list[tuple[datetime, list[str]]] = []
    for text in enriched_texts:
        all_blocks.extend(_blocks(text))
    if not all_blocks:
        return "", 0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    kept = sorted((b for b in all_blocks if b[0] >= cutoff), key=lambda b: b[0])
    return _render(kept), len(kept)


# ───────────────────────── AI 简报 ─────────────────────────

def summarize(window_text: str, model: str = DEFAULT_MODEL, max_tokens: int = MAX_TOKENS) -> str:
    """把窗口文本交给 Claude 生成 STE 英语脉搏简报。空输入返回空串。"""
    if not window_text.strip():
        return ""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("需要设置 ANTHROPIC_API_KEY 才能生成简报")

    from anthropic import Anthropic
    client = Anthropic()
    prompt = prompts.load("pulse_summary.md").format(content=window_text)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [getattr(b, "text", "") for b in getattr(resp, "content", []) or []]
    return "\n".join(p for p in parts if p).strip()


# ───────────────────────── CLI（自测用） ─────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="脉搏简报（STE 英语）")
    ap.add_argument("inputs", nargs="+", type=Path, help="一个或多个 merged.enriched.txt")
    ap.add_argument("--minutes", type=int, default=45, help="回看窗口（分钟）")
    ap.add_argument("--anchor", choices=["now", "last"], default="now",
                    help="窗口锚点：now=真实现在（生产用）；last=最后一条消息时间（离线测试用）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-api", action="store_true", help="只切窗口、不调用 AI（看看喂进去的内容）")
    args = ap.parse_args()

    texts = [p.read_text(encoding="utf-8") for p in args.inputs if p.exists()]
    now = None
    if args.anchor == "last":
        stamps = [t for t in (last_ts(x) for x in texts) if t]
        now = max(stamps) if stamps else None

    window, n = combine_recent(texts, args.minutes, now)
    print(f"[窗口内 {n} 条消息]", file=sys.stderr)
    if n == 0:
        print("（窗口内无新消息，跳过）", file=sys.stderr)
        return
    if args.no_api:
        print(window)
        return
    print(summarize(window, model=args.model))


if __name__ == "__main__":
    main()
