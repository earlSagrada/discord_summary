"""结果回填 & 回测（设计文档模块 6）：把历史信号按 T+1/3/5 交易日的实际走势填进
outcomes 表，并统计各信号/档位/灯色的真实命中率——验证信号到底靠不靠谱。

- 基准价用信号当时的快照价 `signals.price`；未来价用 yfinance 日线的第 h 个交易日收盘。
- 只回填"已经走完 h 个交易日"的信号；不够则留到以后再跑（幂等，重复跑不会重算）。
- 小样本阶段命中率仅供参考，报告会带样本数，别据此激进改参数。

用法：
    python src/backtest.py --backfill        # 回填 outcomes（挂每日计划任务）
    python src/backtest.py --report          # 打印胜率统计
    python src/backtest.py --backfill --report
"""

import argparse
import json
from datetime import datetime

import pandas as pd

import config  # noqa: F401  (load .env + UTF-8)
import market
import store

HORIZONS = [str(h) for h in config.BACKTEST_HORIZONS]


def _signal_date(ts: str) -> "datetime.date | None":
    try:
        return datetime.fromisoformat(ts).date()
    except (TypeError, ValueError):
        return None


def _forward_close(daily: pd.DataFrame, signal_date, horizon: int) -> float | None:
    """信号日之后第 horizon 个交易日的收盘价；数据还没走够则返回 None。"""
    if daily is None or daily.empty:
        return None
    idx = pd.to_datetime(daily.index, utc=True)
    closes = [
        (d.date(), float(c))
        for d, c in zip(idx, daily["Close"])
        if pd.notna(c) and d.date() > signal_date
    ]
    closes.sort(key=lambda x: x[0])
    if len(closes) < horizon:
        return None
    return closes[horizon - 1][1]


def _primary_signal_name(signals_json: str) -> str:
    """signals 列是 ["名字[档] note", ...]，取第一条的名字部分做分组。"""
    try:
        arr = json.loads(signals_json) if signals_json else []
    except (json.JSONDecodeError, TypeError):
        arr = []
    if not arr:
        return "(no signal)"
    first = arr[0]
    return first.split("[")[0].strip() or "(no signal)"


def backfill() -> int:
    """回填所有可算的 (signal, horizon)。返回新填条数。"""
    conn = store.connect()
    done = store.existing_outcomes(conn)
    rows = store.all_signals(conn)
    daily_cache: dict[str, pd.DataFrame] = {}
    filled = 0

    for sid, ts, ticker, price, entry, tier, light, signals_json in rows:
        sdate = _signal_date(ts)
        if sdate is None or not price:
            continue
        pending = [h for h in config.BACKTEST_HORIZONS if (sid, str(h)) not in done]
        if not pending:
            continue
        if ticker not in daily_cache:
            daily_cache[ticker] = market.get_daily(ticker)
        daily = daily_cache[ticker]
        for h in pending:
            fwd = _forward_close(daily, sdate, h)
            if fwd is None:
                continue  # 还没走够 h 个交易日，留待以后
            ret = (fwd / price - 1) * 100
            store.add_outcome(conn, sid, str(h), round(fwd, 4), round(ret, 3))
            filled += 1

    conn.close()
    return filled


def _dedupe(rows: list) -> list:
    """按「票 × 日期 × 信号组合 × 持有期」去重。

    cycle 每 15 分钟就给同一只票再记一条信号，同一天最多 96 条完全一样的记录。
    不去重的话 n 是虚高的、胜率也不是独立样本，统计会严重误导。
    """
    seen, out = set(), []
    for r in rows:
        key = (r[0], r[6], r[3], r[4])  # ticker, day, signals, horizon
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _stats(rows: list, key_fn) -> dict:
    """按 key 分组算 {n, win_rate, avg_ret}。win = ret>0。"""
    groups: dict = {}
    for r in rows:
        k = key_fn(r)
        g = groups.setdefault(k, {"n": 0, "wins": 0, "sum": 0.0})
        g["n"] += 1
        g["sum"] += r[5]  # ret_pct
        if r[5] > 0:
            g["wins"] += 1
    for g in groups.values():
        g["win_rate"] = round(100 * g["wins"] / g["n"], 1) if g["n"] else 0.0
        g["avg_ret"] = round(g["sum"] / g["n"], 2) if g["n"] else 0.0
    return groups


def _print_group(title: str, groups: dict) -> None:
    print(f"\n== {title} ==")
    if not groups:
        print("  （暂无样本）")
        return
    for k in sorted(groups, key=lambda x: -groups[x]["n"]):
        g = groups[k]
        print(f"  {str(k):32} n={g['n']:<4} 胜率 {g['win_rate']:>5}%  平均 {g['avg_ret']:>6}%")


def report_text() -> str:
    """给周报用的纯文本胜率摘要。"""
    conn = store.connect()
    rows = _dedupe(store.outcome_rows(conn))
    conn.close()
    if not rows:
        return "尚无 outcomes 样本（先积累几天信号再回填）。"
    by_sig = _stats(rows, lambda r: f"{_primary_signal_name(r[3])} · T+{r[4]}")
    lines = ["信号胜率（按信号名 × 持有期；已按票×天去重）："]
    for k in sorted(by_sig, key=lambda x: -by_sig[x]["n"]):
        g = by_sig[k]
        lines.append(f"  - {k}: n={g['n']}, 胜率 {g['win_rate']}%, 平均 {g['avg_ret']}%")
    return "\n".join(lines)


def suggestions_text(min_n: int = 15) -> str:
    """基于胜率给"信号引擎调参"的报告级建议（不自动改，人工确认）。

    只对样本量 ≥ min_n 的 (信号名×持有期) 发声，避免小样本误导。
    """
    conn = store.connect()
    rows = _dedupe(store.outcome_rows(conn))
    conn.close()
    if not rows:
        return "样本不足，暂无调参建议。"
    groups = _stats(rows, lambda r: (_primary_signal_name(r[3]), r[4]))
    tips: list[str] = []
    for (name, hz), g in sorted(groups.items(), key=lambda x: -x[1]["n"]):
        if g["n"] < min_n:
            continue
        if g["win_rate"] < 40:
            tips.append(f"⚠ 「{name}」T+{hz} 胜率仅 {g['win_rate']}%（n={g['n']}，平均 {g['avg_ret']}%）"
                        f"→ 考虑降档/加过滤条件或不据此推送。")
        elif g["win_rate"] >= 70 and g["avg_ret"] > 2:
            tips.append(f"✓ 「{name}」T+{hz} 胜率 {g['win_rate']}%、平均 {g['avg_ret']}%（n={g['n']}）"
                        f"→ 表现稳，可维持或提高权重。")
    if not tips:
        return "各信号样本量足够但无明显异常，暂不建议调参。"
    return "信号引擎调参建议（需人工确认后再改，勿据小样本激进调整）：\n" + "\n".join(f"  - {t}" for t in tips)


def report() -> None:
    conn = store.connect()
    raw = store.outcome_rows(conn)
    conn.close()
    rows = _dedupe(raw)
    if not rows:
        print("尚无 outcomes 样本。先跑几天信号、再 --backfill。")
        return
    print(f"回测样本：{len(rows)} 条（按票×天去重；去重前 {len(raw)} 条）")
    _print_group("按信号名 × 持有期", _stats(rows, lambda r: f"{_primary_signal_name(r[3])} · T+{r[4]}"))
    _print_group("按灯色 × 持有期", _stats(rows, lambda r: f"{r[2]} · T+{r[4]}"))
    _print_group("按档位 × 持有期", _stats(rows, lambda r: f"tier {r[1]} · T+{r[4]}"))
    print("\n" + suggestions_text())
    print("\n注：小样本胜率仅供参考；调阈值前先积累足够样本。")


def main() -> None:
    ap = argparse.ArgumentParser(description="信号→结果 回填 + 胜率回测")
    ap.add_argument("--backfill", action="store_true", help="回填 outcomes（T+1/3/5）")
    ap.add_argument("--report", action="store_true", help="打印胜率统计")
    args = ap.parse_args()
    if not (args.backfill or args.report):
        ap.error("至少指定 --backfill 或 --report")
    if args.backfill:
        n = backfill()
        print(f"回填完成：新增 {n} 条 outcome。")
    if args.report:
        report()


if __name__ == "__main__":
    main()
