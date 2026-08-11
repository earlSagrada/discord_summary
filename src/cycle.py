"""每 15 分钟一轮的编排器：入库 → 脉搏简报 → 信号 → 推送到 Discord。

一轮做的事：
  1. （默认）跑一遍 watch_inbox：把 data/inbox 里的新导出去重合并 + enrich，
     按频道落到 data/chats_by_date/<日>/<频道>/merged.enriched.txt。
  2. 把当天各频道 enriched 里"最近 N 分钟"的消息合并成一个窗口。
     窗口内没有新消息 -> 本轮跳过（不调 AI、不推送），省钱不刷屏。
  3. 用便宜的 Claude 生成一段 ASD-STE100 简化技术英语的脉搏简报。
  4. 从当天讨论里抽重点标的，重新打分，得到最新信号卡（STE 英语）。
  5. 简报 + 信号卡拼成一条消息，POST 到 .env 里 DISCORD_WEBHOOK_URL 指向的频道。

挂 Windows 计划任务，每 15 分钟触发一次：
    python src/cycle.py --once

自测（不推送、不入库、锚到最后一条消息）:
    python src/cycle.py --once --dry-run --no-watch --anchor last --date 20260803
"""

import argparse
import sys
from datetime import datetime, timezone

import config
import pulse
import signal_format
import signals as S


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


# 窗口内消息数 ≤ 这个值时，简报里注明「群里没几条新消息」
LOW_ACTIVITY_MAX = 8


def today_str(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def enriched_paths(date_str: str) -> list:
    """当天各频道的 merged.enriched.txt（含旧的扁平布局兜底）。"""
    base = config.CHATS_DIR / date_str
    paths = sorted(base.glob("*/merged.enriched.txt"))  # 新：按频道分目录
    flat = base / "merged.enriched.txt"                 # 旧：扁平
    if flat.exists():
        paths.append(flat)
    return paths


def run_once(args) -> int:
    now = datetime.now(timezone.utc)

    # 1) 入库
    if not args.no_watch:
        import watch_inbox
        done = watch_inbox.load_state()
        n = watch_inbox.scan_once(done)
        log(f"watcher 处理了 {n} 个新导出")

    # 2) 收集当天各频道 enriched，切最近窗口
    date_str = args.date or today_str(now)
    paths = enriched_paths(date_str)
    if not paths:
        log(f"没有 {date_str} 的 enriched 记录，跳过本轮")
        return 0
    texts = [p.read_text(encoding="utf-8") for p in paths]

    anchor = now
    if args.anchor == "last":
        stamps = [t for t in (pulse.last_ts(x) for x in texts) if t]
        anchor = max(stamps) if stamps else now

    window, n_msg = pulse.combine_recent(texts, args.minutes, anchor)
    log(f"窗口内 {n_msg} 条消息（最近 {args.minutes} 分钟，来自 {len(paths)} 个频道）")
    if n_msg == 0 and not args.always:
        log("窗口内无新消息，跳过（不调 AI、不推送）")
        return 0

    low_activity = 0 < n_msg <= LOW_ACTIVITY_MAX

    # 3) 脉搏简报（STE 英语）
    brief = pulse.summarize(window, model=args.model) if n_msg else ""

    # 4) 信号：从当天全部讨论抽标的，重新打分（默认连个股一起，信号更多）
    full_text = "\n".join(texts)
    syms, mentions, unknown = S.resolve_from_text(full_text, all_=not args.focus_only)
    syms = syms[: args.limit]
    cards = []
    if syms:
        cards, run_id = S.analyze(
            syms,
            event_today=args.event_today,
            save=not (args.no_save or args.dry_run),
            source_label=f"cycle:{date_str}",
            mentions=mentions,
        )
    ste_signals = signal_format.format_cards(cards, anchor)

    # 5) 组装消息
    stamp = anchor.strftime("%Y-%m-%d %H:%M UTC")
    header = f"🕒 **Trading pulse — {stamp}**"
    if low_activity:
        header += f"\n_Low activity: only {n_msg} new messages in the last {args.minutes} minutes._"
    body_parts = [header]
    if brief:
        body_parts.append(brief)
    body_parts.append(ste_signals)
    message = "\n\n".join(body_parts).strip()

    if args.dry_run:
        log("dry-run：不推送，以下是将要发送的内容\n")
        print("=" * 60)
        print(message)
        print("=" * 60)
        return 0

    import discord_post
    sent = discord_post.send(message)
    log(f"已推送 {sent} 条到 Discord")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="15 分钟脉搏+信号推送编排器")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（挂计划任务用）")
    ap.add_argument("--minutes", type=int, default=45, help="脉搏窗口（分钟）")
    ap.add_argument("--limit", type=int, default=20, help="最多打分多少个标的")
    ap.add_argument("--focus-only", action="store_true",
                    help="只打分重点 ETP/ETF（默认连个股一起，信号更多）")
    ap.add_argument("--event-today", action="store_true", help="大宏观事件当天：环境不clear")
    ap.add_argument("--model", default=pulse.DEFAULT_MODEL, help="脉搏简报用的模型")
    ap.add_argument("--dry-run", action="store_true", help="不推送、不入库，打印到屏幕")
    ap.add_argument("--no-watch", action="store_true", help="跳过 watcher（用已有 merged 文件）")
    ap.add_argument("--no-save", action="store_true", help="不写 signals.db")
    ap.add_argument("--always", action="store_true", help="窗口内无消息也照常推送")
    ap.add_argument("--anchor", choices=["now", "last"], default="now",
                    help="窗口锚点：now=真实现在（生产）；last=最后一条消息（离线测试）")
    ap.add_argument("--date", default=None, help="强制处理某天 YYYYMMDD（测试用）")
    args = ap.parse_args()

    try:
        sys.exit(run_once(args))
    except RuntimeError as e:
        log(f"本轮失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
