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
import delta
import events
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
    """记录成功推送时间，并清掉停摆/失败告警标志（数据恢复了）。"""
    state = load_state()
    state["last_post_ts"] = ts.isoformat()
    state["stall_alerted"] = False
    state.pop("fail_alerted", None)
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
        detail = f"最后一条消息是 {newest.strftime('%m-%d %H:%M UTC')}（{age_min:.0f} 分钟前）。"
    else:
        detail = "没有可用的近期消息。"
    alert = ("⚠️ **导出好像停了。** " + detail + "\n"
             "没有新聊天进来。请检查那个 Discord 标签页（可能被关了、被浏览器丢弃了、"
             "或者电脑睡眠了），并确认油猴脚本的「定时导出」是开着的。")
    try:
        import discord_post
        discord_post.send(alert)
        log("已发送停摆告警到 Discord")
    except Exception as e:  # 告警失败不影响主流程
        log(f"停摆告警发送失败：{type(e).__name__}: {e}")
    state["stall_alerted"] = True
    save_state(state)


def maybe_alert_failure(exc: Exception, args) -> None:
    """本轮异常失败时往 Discord 发一次告警（同一类错误只发一次，避免刷屏）。

    没有这个的话，像 API key 失效这种错误会**静默**失败几十轮都没人知道
    （只写进 cycle.log），推送就那么"消失"了。
    """
    if args.dry_run:
        return
    kind = type(exc).__name__
    state = load_state()
    if state.get("fail_alerted") == kind:
        return  # 同类错误已告警过

    msg = str(exc)[:300]
    hint = ""
    if "authentication" in msg.lower() or "401" in msg:
        hint = "\n看起来是 ANTHROPIC_API_KEY 失效或过期了，请重新生成一个填进 .env。"
    elif "webhook" in msg.lower() or "404" in msg:
        hint = "\nDiscord webhook 地址可能错了或被删了，请检查 .env。"
    alert = (f"🛑 **脉搏推送这轮失败了。**（{kind}）\n```{msg}```"
             f"{hint}\n在你修好之前不会再有新推送。详情见 data/cycle.log。")
    try:
        import discord_post
        discord_post.send(alert)
        log(f"已发送失败告警到 Discord（{kind}）")
        state["fail_alerted"] = kind
        save_state(state)
    except Exception as e:  # 告警本身失败就只记日志
        log(f"失败告警发送失败：{type(e).__name__}: {e}")


def maybe_trigger_substack() -> None:
    """发现新的 Substack 复盘就**后台**跑一次系统校准。

    为什么用子进程而不是直接调用：那条流水线要跑周期复盘 + opus 优化，
    几分钟起步。15 分钟的脉搏推送绝不能为它等着。
    检测本身只是列目录 + 读一个小 JSON，每轮的开销可以忽略。
    """
    try:
        import substack
        fresh = substack.new_posts()
        if not fresh:
            return
        import subprocess
        script = config.SRC_DIR / "substack_pipeline.py"
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
            getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(config.PROJECT_ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        log(f"发现新 Substack 复盘（{'、'.join(p.stem for p in fresh)}）"
            f"→ 已在后台启动系统校准，详情见 data/pipeline.log")
    except Exception as e:  # 触发失败绝不能影响本轮推送
        log(f"Substack 校准触发失败（{type(e).__name__}: {e}），下轮再试")


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

    # 1c) 新的 Substack 复盘落地了？→ 后台重新校准系统（不阻塞本轮推送）
    if not (args.dry_run or args.no_save):
        maybe_trigger_substack()

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

    # 3b) 先取"本轮写库前"的今日状态：今天已经推过什么、每只票之前什么样。
    #     必须在 S.analyze(save=True) 之前拿，否则本轮自己的记录会污染基准。
    try:
        prior = delta.snapshot(now)
    except Exception as e:
        log(f"变化快照不可用（{type(e).__name__}: {e}），本轮按首轮处理")
        prior = {"day": today_str(now), "history": {}, "mentions": {}, "rounds": []}
    prior_briefs = delta.prior_context(prior)
    thread = delta.thread_hint(prior)
    if prior.get("rounds"):
        log(f"今天已推送 {len(prior['rounds'])} 轮，主线：{thread or '（未确立）'}"
            f" → 本轮只讲增量")

    # 4) 脉搏简报（中文，带"今天已经说过什么"的记忆）；追补模式给更大的输出上限
    brief = pulse.summarize(
        window, model=args.model,
        max_tokens=4000 if catchup else pulse.MAX_TOKENS,
        prior_briefs=prior_briefs,
        thread=thread,
    ) if n_msg else ""

    # 5) 信号：从当天全部讨论抽标的，重新打分（默认连个股一起，信号更多）
    full_text = "\n".join(texts)
    syms, mentions, unknown = S.resolve_from_text(full_text, all_=not args.focus_only)
    syms = syms[: args.limit]

    # 5a) 宏观事件：自动判断今天是否大宏观日（--event-today 仍可手动强制）
    ev_names = events.event_names()
    event_today = args.event_today or bool(ev_names)
    if ev_names:
        log(f"今日宏观事件：{', '.join(ev_names)} → 环境标为不 clear")

    # 5b) 每个标的最后一次被提及的时间 → 标"聊天已不热"
    import extract
    mention_times = extract.last_mention_times(texts)

    cards = []
    if syms:
        cards, run_id = S.analyze(
            syms,
            event_today=event_today,
            save=not (args.no_save or args.dry_run),
            source_label=f"cycle:{today_str(now)}",
            mentions=mentions,
            now=anchor,
            mention_times=mention_times,
            event_names=ev_names,
        )
    ste_signals = signal_format.format_cards(cards, anchor)

    # 5c) 确定性的「变化」块：灯色/新票/热度/价格进展，都是算出来的，不让模型猜
    try:
        change = delta.compute(prior, cards, mentions)
        change_block = delta.render(change)
    except Exception as e:
        log(f"变化计算失败（{type(e).__name__}: {e}），本轮不带变化块")
        change, change_block = {}, ""
    if change_block:
        log(f"变化：新票 {len(change.get('new', []))}、灯色变动 "
            f"{len(change.get('lights', []))}、升温 {len(change.get('heat', []))}")

    # 6) 组装消息（中文；各块之间只换行、不留空行 —— 用户要求更紧凑）
    stamp = anchor.strftime("%m-%d %H:%M UTC")
    if catchup:
        hrs = win_min / 60
        header = (f"📣 **补发：你错过了过去 {hrs:.1f} 小时** · {stamp}\n"
                  f"_导出中断了一段时间，这条覆盖整个空档。_")
    else:
        round_no = change.get("round_no", 1)
        header = f"🕒 **交易脉搏** · {stamp}" + (f" · 今日第 {round_no} 条" if round_no > 1 else "")
        if low_activity:
            header += f"\n_群里比较冷清：最近 {args.minutes} 分钟只有 {n_msg} 条新消息。_"
    if ev_names:
        header += f"\n⚠️ _今天有 {'、'.join(ev_names)}，环境不明朗，仓位放小。_"
    body_parts = [header]
    if change_block:
        body_parts.append(change_block)
    if brief:
        body_parts.append(brief)
    body_parts.append(ste_signals)
    message = "\n".join(body_parts).strip()

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
    remember_round(now, n_msg, cards, brief, thread, args)
    return 0


def remember_round(now, n_msg, cards, brief, prev_thread, args) -> None:
    """把这轮推出去的内容记下来，下一轮才知道"哪些话已经说过了"。"""
    if args.no_save:
        return
    try:
        import store
        conn = store.connect()
        try:
            store.add_pulse_round(
                conn, day=today_str(now), n_msg=n_msg,
                tickers=[c["ticker"] for c in cards],
                thread=pulse.extract_thread(brief) or prev_thread,
                brief=brief,
            )
        finally:
            conn.close()
    except Exception as e:  # 记忆写失败不该影响推送本身
        log(f"推送记忆写入失败（{type(e).__name__}: {e}）")


def main() -> None:
    ap = argparse.ArgumentParser(description="15 分钟脉搏+信号推送编排器")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（挂计划任务用）")
    ap.add_argument("--minutes", type=int, default=45, help="脉搏窗口（分钟）")
    ap.add_argument("--limit", type=int, default=20, help="最多打分多少个标的")
    ap.add_argument("--focus-only", action="store_true",
                    help="只打分重点 ETP/ETF（默认连个股一起，信号更多）")
    ap.add_argument("--event-today", action="store_true",
                    help="强制把今天标为大宏观日（events.py 已自动判断，这是手动覆盖）")
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
        maybe_alert_failure(e, args)
        sys.exit(1)


if __name__ == "__main__":
    main()
