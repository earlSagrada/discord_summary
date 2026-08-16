"""脉搏简报：从 merged.enriched.txt 里切出"最近 N 分钟"的新消息，
交给便宜的 Claude（haiku）生成一段 ASD-STE100 简化技术英语的"现在在聊什么"。

- 时间切片纯本地做（读 enriched 文本里的日期头 + HH:MM 时间行，均为 UTC）。
- 窗口内没有消息就返回空串 -> cycle.py 据此跳过 AI 调用与推送（省钱、不刷屏）。
- prompt 放在 prompts/pulse_summary.md，改措辞不用动代码。

用法（自测）:
    python src/pulse.py data/chats_by_date/20260803/frank/merged.enriched.txt --minutes 60 --anchor last
    python src/pulse.py --from 20260813 --to 20260814 --no-api
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
MAX_TOKENS = 2500

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


def combine_range(
    enriched_texts: list[str],
    start: datetime,
    end: datetime,
) -> tuple[str, int]:
    """把任意 UTC 时间段 [start, end) 内的消息合并、按时间排序。"""
    all_blocks: list[tuple[datetime, list[str]]] = []
    for text in enriched_texts:
        all_blocks.extend(_blocks(text))
    kept = sorted((b for b in all_blocks if start <= b[0] < end), key=lambda b: b[0])
    return _render(kept), len(kept)


def _enriched_paths(date_str: str) -> list[Path]:
    """某一天各频道的 merged.enriched.txt（含旧扁平布局兜底）。"""
    base = config.CHATS_DIR / date_str
    if not base.exists():
        return []
    paths = sorted(base.glob("*/merged.enriched.txt"))
    flat = base / "merged.enriched.txt"
    if flat.exists():
        paths.append(flat)
    return paths


def gather_range_texts(start: datetime, end: datetime) -> list[str]:
    """收集 [start, end] 覆盖到的 UTC 日期里所有 enriched 文本。"""
    texts: list[str] = []
    cur = start.date()
    while cur <= end.date():
        for p in _enriched_paths(cur.strftime("%Y%m%d")):
            try:
                texts.append(p.read_text(encoding="utf-8"))
            except OSError:
                pass
        cur += timedelta(days=1)
    return texts


# ───────────────────────── VIP 发言人 ─────────────────────────

def load_vips() -> list[str]:
    """读 prompts/vip_speakers.txt（一行一个名字，# 注释、空行忽略）。"""
    path = prompts.PROMPTS_DIR / "vip_speakers.txt"
    if not path.exists():
        return []
    names = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


# ───────────────────────── AI 简报 ─────────────────────────

def summarize(
    window_text: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = MAX_TOKENS,
    vips: list[str] | None = None,
) -> str:
    """把窗口文本交给 Claude 生成 STE 英语脉搏简报。空输入返回空串。"""
    if not window_text.strip():
        return ""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("需要设置 ANTHROPIC_API_KEY 才能生成简报")

    vips = vips if vips is not None else load_vips()
    vip_names = "\n".join(f"- {v}" for v in vips) if vips else "- (none set yet)"

    from anthropic import Anthropic
    client = Anthropic()
    prompt = prompts.load("pulse_summary.md").format(content=window_text, vip_names=vip_names)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [getattr(b, "text", "") for b in getattr(resp, "content", []) or []]
    return "\n".join(p for p in parts if p).strip()


# ───────────────────────── CLI（自测用） ─────────────────────────

def _parse_utc_stamp(raw: str) -> datetime:
    """解析 YYYYMMDD / YYYYMMDDHHMM，按 UTC 处理。"""
    fmt = "%Y%m%d" if re.fullmatch(r"\d{8}", raw) else "%Y%m%d%H%M"
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise argparse.ArgumentTypeError("时间格式需为 YYYYMMDD 或 YYYYMMDDHHMM") from e


def _parse_last_spec(raw: str) -> timedelta:
    """解析 90m / 6h / 3d 这类回看窗口。"""
    m = re.fullmatch(r"(\d+)([mhd])", raw.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("--last 需为 90m / 6h / 3d 这类格式")
    n = int(m.group(1))
    if n <= 0:
        raise argparse.ArgumentTypeError("--last 必须大于 0")
    unit = m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


def _fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def signal_block(window_text: str, now: datetime | None = None, limit: int = 20) -> str:
    """给一段窗口文本抽标的、打分（含期权确认），渲染成 STE 信号卡。

    行情/期权用的是**当前**数据（非历史那天），所以下面会加一句说明。
    """
    import signal_format
    import signals as S

    syms, mentions, _ = S.resolve_from_text(window_text, all_=True)
    syms = syms[:limit]
    if not syms:
        return ""

    try:
        import events
        ev_names = events.event_names()
    except Exception:
        ev_names = []

    try:
        import extract
        mention_times = extract.last_mention_times([window_text])
    except Exception:
        mention_times = {}

    cards, _ = S.analyze(
        syms, event_today=bool(ev_names), save=False,
        source_label="pulse-range", mentions=mentions,
        now=now, mention_times=mention_times, event_names=ev_names,
    )
    body = signal_format.format_cards(cards, now)
    note = "_Note: the signals and options use today's market data, not the data of the past days._"
    return f"{note}\n\n{body}"


def main() -> None:
    ap = argparse.ArgumentParser(description="脉搏简报（STE 英语）")
    ap.add_argument("inputs", nargs="*", type=Path, help="一个或多个 merged.enriched.txt")
    ap.add_argument("--minutes", type=int, default=45, help="回看窗口（分钟）")
    ap.add_argument("--anchor", choices=["now", "last"], default="now",
                    help="窗口锚点：now=真实现在（生产用）；last=最后一条消息时间（离线测试用）")
    ap.add_argument("--last", type=_parse_last_spec, default=None,
                    help="历史回看：以现在为终点，如 90m / 6h / 3d")
    ap.add_argument("--from", dest="from_", type=_parse_utc_stamp, default=None,
                    help="历史起点 UTC：YYYYMMDD 或 YYYYMMDDHHMM")
    ap.add_argument("--to", type=_parse_utc_stamp, default=None,
                    help="历史终点 UTC（可选）：YYYYMMDD 或 YYYYMMDDHHMM")
    ap.add_argument("--signals", action="store_true",
                    help="简报后附上这段时间点到的票的带期权信号卡（用当前行情/期权数据）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-api", action="store_true", help="只切窗口、不调用 AI（看看喂进去的内容）")
    ap.add_argument("--post", action="store_true", help="把历史简报推送到 Discord webhook")
    args = ap.parse_args()

    if args.last is not None and args.from_ is not None:
        ap.error("--last 和 --from 只能选一个")
    if args.to is not None and args.from_ is None:
        ap.error("--to 需搭配 --from 使用")

    range_mode = args.last is not None or args.from_ is not None
    start = end = None
    if args.last is not None:
        end = datetime.now(timezone.utc)
        start = end - args.last
    elif args.from_ is not None:
        start = args.from_
        end = args.to or datetime.now(timezone.utc)

    if range_mode:
        assert start is not None and end is not None
        if start >= end:
            ap.error("历史窗口需满足 start < end")
        texts = gather_range_texts(start, end)
        window, n = combine_range(texts, start, end)
    else:
        if not args.inputs:
            ap.error("需提供输入文件，或使用 --last / --from 自动收集历史记录")
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
    max_tokens = MAX_TOKENS
    if range_mode and end - start > timedelta(hours=3):
        max_tokens = 4000
    brief = summarize(window, model=args.model, max_tokens=max_tokens)

    sig_text = ""
    if args.signals:
        print("[抽标的、打分（含期权）…]", file=sys.stderr)
        sig_text = signal_block(window, now=end)

    body = brief if not sig_text else f"{brief}\n\n{sig_text}"
    if args.post and range_mode:
        import discord_post
        header = f"📜 **Historical pulse — {_fmt_utc(start)} → {_fmt_utc(end)} UTC**"
        sent = discord_post.send(f"{header}\n\n{body}")
        print(f"[已推送 {sent} 条到 Discord]", file=sys.stderr)
        return
    print(body)


if __name__ == "__main__":
    main()
