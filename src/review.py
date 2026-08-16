"""周期性复盘（每周/隔几天）：总结"有价值的发言人"和"成功投资的分析方法"，
并结合信号引擎的胜率统计给出调参建议。对应用户第二个诉求。

流程：
  1. 收集最近 N 天各频道 merged.enriched.txt（多频道合并）。
  2. 取 backtest 的胜率统计 + 调参建议。
  3. 交给较强的 Claude（sonnet）出一份中文 Markdown 复盘报告。
  4. 完整报告写 trade_notes/reviews/<日期>-review.md；
     其中「TL;DR」段落作为短摘要推送到 Discord；
     「VIP 名单建议」段落抽到 prompts/vip_suggestions.md（人工确认后再改 vip_speakers.txt）。

用法：
    python src/review.py                 # 最近 7 天，出报告 + 推送
    python src/review.py --days 5 --no-post
    python src/review.py --no-api        # 只拼 prompt 看看喂进去什么（离线自测）
"""

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import prompts
import pulse

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 8000
REVIEWS_DIR = config.PROJECT_ROOT / "trade_notes" / "reviews"


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


def gather_texts(days: int, now: datetime | None = None) -> tuple[str, list[str]]:
    """把最近 days 天各频道 enriched 合并成一段（带频道/日期标注）。"""
    now = now or datetime.now(timezone.utc)
    chunks, seen_days = [], []
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y%m%d")
        paths = _enriched_paths(d)
        if not paths:
            continue
        seen_days.append(d)
        for p in paths:
            try:
                chunks.append(f"\n\n### {d} / {p.parent.name}\n" + p.read_text(encoding="utf-8"))
            except OSError:
                pass
    seen_days.sort()
    return "".join(chunks), seen_days


def _section(md: str, heading: str) -> str | None:
    """从 Markdown 里抽出某个 '## 标题' 到下一个 '## ' 之间的内容（含标题）。"""
    lines = md.splitlines()
    out, capture = [], False
    for line in lines:
        if line.strip().startswith("## "):
            if capture:
                break
            if heading in line:
                capture = True
        if capture:
            out.append(line)
    return "\n".join(out).strip() if out else None


def summarize(content: str, winrate: str, vips: list[str], days: int,
              period: str, model: str = DEFAULT_MODEL) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("需要设置 ANTHROPIC_API_KEY 才能生成复盘")
    vip_names = "\n".join(f"- {v}" for v in vips) if vips else "- (none set yet)"
    prompt = prompts.load("weekly_review.md").format(
        content=content, winrate=winrate, vips=vip_names, days=days, period=period)
    from anthropic import Anthropic
    client = Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [getattr(b, "text", "") for b in getattr(resp, "content", []) or []]
    return "\n".join(p for p in parts if p).strip()


def run(days: int = 7, model: str = DEFAULT_MODEL, post: bool = True,
        no_api: bool = False, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    content, seen = gather_texts(days, now)
    if not content.strip():
        print("最近没有可用的聊天记录，跳过复盘。")
        return ""
    period = f"{seen[0]}–{seen[-1]}" if seen else now.strftime("%Y%m%d")

    import backtest
    try:
        winrate = backtest.report_text() + "\n\n" + backtest.suggestions_text()
    except Exception as e:  # 回测不可用不影响复盘主体
        winrate = f"（胜率统计不可用：{type(e).__name__}）"

    vips = pulse.load_vips()

    # 拼 prompt 预览（离线自测）
    if no_api:
        preview = prompts.load("weekly_review.md").format(
            content=content[:2000] + "…(截断)", winrate=winrate,
            vips="\n".join(f"- {v}" for v in vips) or "- (none)", days=days, period=period)
        print(preview)
        return ""

    report_md = summarize(content, winrate, vips, days, period, model)

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEWS_DIR / f"{now.strftime('%Y%m%d')}-review.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"复盘报告已写入 {out_path}")

    # VIP 名单建议 → 单独存档，供人工确认后再改 vip_speakers.txt
    vip_sec = _section(report_md, "VIP 名单建议")
    if vip_sec:
        sug_path = prompts.PROMPTS_DIR / "vip_suggestions.md"
        sug_path.write_text(f"# 由 review.py 生成（{period}），人工确认后再改 vip_speakers.txt\n\n{vip_sec}\n",
                            encoding="utf-8")
        print(f"VIP 名单建议已写入 {sug_path}")

    # TL;DR → 推送 Discord
    if post:
        tldr = _section(report_md, "TL;DR") or report_md[:1500]
        msg = f"🧭 **交易群周期复盘 {period}**\n\n{tldr}\n\n_（完整报告见本地 trade_notes/reviews/）_"
        try:
            import discord_post
            discord_post.send(msg)
            print("TL;DR 已推送到 Discord")
        except Exception as e:
            print(f"推送失败：{type(e).__name__}: {e}")

    return report_md


def main() -> None:
    ap = argparse.ArgumentParser(description="周期性复盘（发言人价值 + 成功方法 + 信号校准）")
    ap.add_argument("--days", type=int, default=7, help="回看天数")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="用哪个模型（默认 sonnet）")
    ap.add_argument("--no-post", action="store_true", help="不推送 Discord，只写文件")
    ap.add_argument("--no-api", action="store_true", help="只拼 prompt 不调 AI（离线自测）")
    args = ap.parse_args()
    run(days=args.days, model=args.model, post=not args.no_post, no_api=args.no_api)


if __name__ == "__main__":
    main()
