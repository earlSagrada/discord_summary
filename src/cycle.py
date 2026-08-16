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
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone

import config
import pulse
import signal_format
import signals as S

CYCLE_LOG = config.DATA_DIR / "cycle.log"
STATE_FILE = config.DATA_DIR / "cycle_state.json"


def log(msg: str) -> None:
    """打印到 stdout，并追加到 data/cycle.log（永久留存，便于事后排查「暂停」原因）。"""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with CYCLE_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# 窗口内消息数 ≤ 这个值时，简报里注明「群里没几条新消息」
LOW_ACTIVITY_MAX = 8
# 停摆恢复后，追补摘要最多回看这么久（分钟）——对齐油猴脚本 maxBackfillHours=12
MAX_CATCHUP_MIN = 12 * 60
# 最新消息超过这么久没更新 → 判定导出停摆，往 Discord 发一次告警（每次停摆只发一次）
STALL_ALERT_MIN = 90


def load_state() -> dict:
    """读状态：last_post_ts（上次成功推送 UTC）+ stall_alerted（本次停摆是否已告警）。"""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_last_post() -> datetime | None:
    """读上次成功推送的 UTC 时间（= 输出频道里我们最后一条消息的时间）。"""
    ts = load_state().get("last_post_ts")
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def save_last_post(ts: datetime) -> None:
    """记录成功推送时间，并清掉停摆告警标志（数据恢复了）。"""
    state = load_state()
    state["last_post_ts"] = ts.isoformat()
    state["stall_alerted"] = False
    save_state(state)


def today_str(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def enriched_paths(date_str: str) -> list:
    """某一天各频道的 merged.enriched.txt（含旧的扁平布局兜底）。"""
    base = config.CHATS_DIR / date_str
    paths = sorted(base.glob("*/merged.enriched.txt"))  # 新：按频道分目录
    flat = base / "merged.enriched.txt"                 # 旧：扁平
    if flat.exists():
        paths.append(flat)
    return paths


def enriched_paths_for_window(now: datetime, win_min: float, forced_date: str | None) -> list:
    """窗口可能跨 UTC 日界（追补最多 12h），把窗口覆盖到的每一天的文件都收进来。"""
    if forced_date:
        return enriched_paths(forced_date)
    start = now - timedelta(minutes=win_min)
    days, cur = [], start.date()
    while cur <= now.date():
        days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    paths = []
    for d in days:
        paths.extend(enriched_paths(d))
    return paths


def newest_message_time(now: datetime) -> datetime | None:
    """扫今天+昨天的 enriched，找出最新一条消息的时间（用于停摆告警，不受窗口限制）。"""
    paths = []
    for d in (now, now - timedelta(days=1)):
        paths.extend(enriched_paths(d.strftime("%Y%m%d")))
    stamps = []
    for p in paths:
        try:
            t = pulse.last_ts(p.read_text(encoding="utf-8"))
            if t:
                stamps.append(t)
        except OSError:
            pass
    return max(stamps) if stamps else None


def maybe_alert_stall(now: datetime, newest: datetime | None, args) -> None:
    """导出停摆时，往 Discord 发一次告警（每次停摆只发一次，恢复后自动清标志）。"""
    if args.dry_run or args.always or args.anchor != "now":
        return
    age_min = (now - newest).total_seconds() / 60 if newest else None
    if age_min is not None and age_min <= STALL_ALERT_MIN:
        return  # 数据还新鲜，无需告警
    state = load_state()
    if state.get("stall_alerted"):
        return  # 本次停摆已经告警过，不刷屏

    if newest:
        detail = f"The last message is from {newest.strftime('%Y-%m-%d %H:%M UTC')} ({age_min:.0f} min ago)."
    else:
        detail = "No recent messages are available."
    alert = ("⚠️ **Exporter looks stalled.** " + detail + "\n"
             "No new chat is coming in. Please check the Discord tab "
             "(it may be closed, discarded, or the PC was asleep) and make sure "
             "the userscript's timed export is ON.")
    try:
        import discord_post
        discord_post.send(alert)
        log("已发送停摆告警到 Discord")
    except Exception as e:  # 告警失败不影响主流程
        log(f"停摆告警发送失败：{type(e).__name__}: {e}")
    state["stall_alerted"] = True
    save_state(state)


def run_once(args) -> int:
    now = datetime.now(timezone.utc)
    log(f"=== run start (UTC {now.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    # 1) 入库
    if not args.no_watch:
        import watch_inbox
        done = watch_inbox.load_state()
        n = watch_inbox.scan_once(done)
        log(f"watcher 处理了 {n} 个新导出")

    # 1b) 停摆检测：最新消息太旧就往 Discord 发一次告警（让你知道该去重启标签页）
    newest = newest_message_time(now)
    maybe_alert_stall(now, newest, args)

    # 2) 决定窗口：正常 = args.minutes；若距上次推送有缺口 -> 追补窗口（上限 12h）
    win_min = float(args.minutes)
    catchup = False
    last_post = None if args.always else load_last_post()
    if last_post and args.anchor == "now":
        gap_min = (now - last_post).total_seconds() / 60
        if gap_min > args.minutes:
            win_min = min(gap_min, MAX_CATCHUP_MIN)
            catchup = True
            log(f"检测到缺口：上次推送在 {last_post.strftime('%m-%d %H:%M UTC')}"
                f"（{gap_min:.0f} 分钟前）→ 追补窗口 {win_min:.0f} 分钟（上限 {MAX_CATCHUP_MIN}）")

    # 3) 收集窗口覆盖到的各天/各频道 enriched
    paths = enriched_paths_for_window(now, win_min, args.date)
    if not paths:
        log("结果：跳过（没有可用的 enriched 记录）")
        return 0
    texts = [p.read_text(encoding="utf-8") for p in paths]

    anchor = now
    if args.anchor == "last":
        stamps = [t for t in (pulse.last_ts(x) for x in texts) if t]
        anchor = max(stamps) if stamps else now

    # 最近一条消息距今多久 —— 判断「导出是否停摆」的关键信号
    latest_stamps = [t for t in (pulse.last_ts(x) for x in texts) if t]
    if latest_stamps:
        latest = max(latest_stamps)
        age_min = (now - latest).total_seconds() / 60
        log(f"最近一条消息：{latest.strftime('%H:%M UTC')}（{age_min:.0f} 分钟前）")
        if args.anchor == "now" and age_min > args.minutes:
            log(f"提示：最近消息已超过窗口 {args.minutes} 分钟，导出可能停摆"
                f"（Discord 标签页被后台丢弃/电脑睡眠？）")

    window, n_msg = pulse.combine_recent(texts, win_min, anchor)
    log(f"窗口内 {n_msg} 条消息（最近 {win_min:.0f} 分钟，来自 {len(paths)} 个文件"
        f"{'，追补模式' if catchup else ''}）")
    if n_msg == 0 and not args.always:
        log("结果：跳过（窗口内无新消息，不调 AI、不推送）")
        return 0

    low_activity = (not catchup) and 0 < n_msg <= LOW_ACTIVITY_MAX

    # 4) 脉搏简报（STE 英语）；追补模式给更大的输出上限
    brief = pulse.summarize(
        window, model=args.model,
        max_tokens=4000 if catchup else pulse.MAX_TOKENS,
    ) if n_msg else ""

    # 5) 信号：从当天全部讨论抽标的，重新打分（默认连个股一起，信号更多）
    full_text = "\n".join(texts)
    syms, mentions, unknown = S.resolve_from_text(full_text, all_=not args.focus_only)
    syms = syms[: args.limit]
    cards = []
    if syms:
        cards, run_id = S.analyze(
            syms,
            event_today=args.event_today,
            save=not (args.no_save or args.dry_run),
            source_label=f"cycle:{today_str(now)}",
            mentions=mentions,
        )
    ste_signals = signal_format.format_cards(cards, anchor)

    # 6) 组装消息
    stamp = anchor.strftime("%Y-%m-%d %H:%M UTC")
    if catchup:
        hrs = win_min / 60
        header = (f"📣 **Catch-up — you missed the last {hrs:.1f} hours** ({stamp})\n"
                  f"_The export paused for a while. This message covers the whole gap._")
    else:
        header = f"🕒 **Trading pulse — {stamp}**"
        if low_activity:
            header += f"\n_Low activity: only {n_msg} new messages in the last {args.minutes} minutes._"
    body_parts = [header]
    if brief:
        body_parts.append(brief)
    body_parts.append(ste_signals)
    message = "\n\n".join(body_parts).strip()

    if args.dry_run:
        log("结果：dry-run（不推送），以下是将要发送的内容\n")
        print("=" * 60)
        print(message)
        print("=" * 60)
        return 0

    import discord_post
    sent = discord_post.send(message)
    save_last_post(now)
    log(f"结果：已推送 {sent} 条到 Discord{'（追补）' if catchup else ''}")
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
    except SystemExit:
        raise
    except Exception as e:
        log(f"结果：本轮失败 {type(e).__name__}: {e}")
        log("traceback: " + traceback.format_exc().replace("\n", " | "))
        sys.exit(1)


if __name__ == "__main__":
    main()
