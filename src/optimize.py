"""自我优化：让系统自己调参数、增删方法论，并把**决策过程**完整记录下来。

和 selfreview.py 的分工：
  - selfreview（每天，sonnet）→ 学**分析方法**：今天该怎么看盘。
  - optimize（每周，opus）  → 改**系统本身**：哪些信号是噪音、阈值该松该紧、
    playbook 里哪条该提权、哪条该删。

为什么值得用贵模型：这一步要读胜率统计、权衡取舍、还要论证"为什么不选别的做法"。
一周一次，成本可以忽略，但判断质量直接决定系统会不会越调越差。

**安全设计**（这是自动改自己的系统，护栏比功能重要）：
  1. AI **只能改 data/tunables.json**，永远碰不到 Python 源码；
  2. 每个参数在 config.TUNABLES 里有上下界，越界值会被夹住或拒绝；
  3. 每次应用前自动快照上一版 → `--revert` 一键回滚；
  4. 决策记录写 trade_notes/optimizations/，含**否决的替代方案**；
  5. 样本量不够（n < MIN_SAMPLES）时 prompt 明令禁止调参。

用法：
    python src/optimize.py                # 跑一次：分析 → 应用 → 记录 → 推送
    python src/optimize.py --dry-run      # 只出报告，不改任何东西
    python src/optimize.py --no-post
    python src/optimize.py --revert       # 回滚到上一版参数
    python src/optimize.py --show         # 看当前生效的参数覆盖
    python src/optimize.py --no-api       # 只打印 prompt（不花钱）
"""

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone

import config
import prompts
import pulse

DEFAULT_MODEL = "claude-opus-4-5-20251101"
MAX_TOKENS = 12000
OPT_DIR = config.PROJECT_ROOT / "trade_notes" / "optimizations"
SNAPSHOT_FILE = config.DATA_DIR / "tunables.bak.json"
# 低于这个样本数不许用来调参（和 prompt 里写给 AI 的是同一个数）
MIN_SAMPLES = 15
PLAYBOOK_MAX = 20


# ───────────────────────── 收集材料 ─────────────────────────

def winrate_text() -> str:
    try:
        import backtest
        return backtest.report_text()
    except Exception as e:
        return f"（胜率统计不可用：{type(e).__name__}: {e}）"


def tunables_text() -> str:
    lines = []
    for name, spec in config.TUNABLES.items():
        cur = getattr(config, name)
        default = config.tunable_defaults()[name]
        tag = "" if cur == default else f"（出厂默认 {default}，已被调整过）"
        lines.append(f"- `{name}` = {cur}｜允许区间 [{spec['min']}, {spec['max']}]"
                     f"｜{spec['desc']}{tag}")
    return "\n".join(lines)


def recent_reviews(limit: int = 3, max_chars: int = 12000) -> str:
    if not OPT_DIR.parent.exists():
        return "（还没有复盘报告。）"
    review_dir = config.PROJECT_ROOT / "trade_notes" / "reviews"
    files = sorted(review_dir.glob("*.md"))[-limit:] if review_dir.exists() else []
    if not files:
        return "（还没有复盘报告。）"
    chunks = [f"### {p.name}\n{p.read_text(encoding='utf-8')}" for p in files]
    return "\n\n".join(chunks)[-max_chars:]


def history_text(limit: int = 3, max_chars: int = 6000) -> str:
    if not OPT_DIR.exists():
        return "（这是第一次优化，没有历史记录。）"
    files = sorted(OPT_DIR.glob("*.md"))[-limit:]
    if not files:
        return "（这是第一次优化，没有历史记录。）"
    chunks = [f"### {p.name}\n{p.read_text(encoding='utf-8')}" for p in files]
    return "\n\n".join(chunks)[-max_chars:]


def vips_text() -> str:
    """当前 VIP 名单 + 待确认建议。少了这段，AI 会对名单状态瞎猜。"""
    try:
        import vips
        _, names = vips.load_list()
        sug = vips.parse_suggestions()
        lines = ["当前名单：" + ("、".join(names) if names else "（空）")]
        if sug["add"]:
            lines.append("复盘建议加入：" + "、".join(sug["add"]))
        if sug["remove"]:
            in_list = [n for n in sug["remove"] if n in names]
            lines.append("复盘建议移除：" + "、".join(sug["remove"])
                         + ("" if in_list else "（注意：这些人本来就不在名单里，无需操作）"))
        if not sug["add"] and not sug["remove"]:
            lines.append("本期没有增删建议。")
        return "\n".join(lines)
    except Exception as e:
        return f"（VIP 名单读取失败：{type(e).__name__}: {e}）"
        return "（这是第一次优化，没有历史记录。）"
    files = sorted(OPT_DIR.glob("*.md"))[-limit:]
    if not files:
        return "（这是第一次优化，没有历史记录。）"
    chunks = [f"### {p.name}\n{p.read_text(encoding='utf-8')}" for p in files]
    return "\n\n".join(chunks)[-max_chars:]


# ───────────────────────── 解析 AI 产出 ─────────────────────────

_PARAM_RE = re.compile(r"###\s*PARAM:\s*(\w+)(.*?)(?=\n###|\Z)", re.S)
_PB_RE = re.compile(r"###\s*PLAYBOOK:\s*(add|remove|promote)\b(.*?)(?=\n###|\Z)", re.S | re.I)


def _field(block: str, name: str) -> str:
    m = re.search(rf"^[-*]\s*{name}\s*[:：]\s*(.+?)(?=\n[-*]\s*\S+\s*[:：]|\Z)",
                  block, re.S | re.M)
    return m.group(1).strip() if m else ""


def parse_params(report: str) -> list[dict]:
    out = []
    for name, block in _PARAM_RE.findall(report):
        raw = _field(block, "建议")
        m = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not m:
            continue
        out.append({
            "name": name,
            "value": m.group(0),
            "reason": _field(block, "依据"),
            "rejected": _field(block, "否决的替代方案"),
        })
    return out


def parse_playbook_ops(report: str) -> list[dict]:
    out = []
    for op, block in _PB_RE.findall(report):
        content = _field(block, "内容")
        if content:
            out.append({"op": op.lower(), "content": content.strip().strip("`\"“”"),
                        "reason": _field(block, "依据")})
    return out


# ───────────────────────── 应用改动 ─────────────────────────

def current_overrides() -> dict:
    if config.TUNABLES_FILE.exists():
        try:
            return json.loads(config.TUNABLES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"values": {}, "history": []}


def apply_params(params: list[dict], day: str) -> list[str]:
    """把参数改动写进 tunables.json（先快照）。返回人类可读的改动说明。"""
    if not params:
        return []
    data = current_overrides()
    values = dict(data.get("values") or {})
    applied = []
    for p in params:
        name = p["name"]
        if name not in config.TUNABLES:
            applied.append(f"⚠ 跳过 {name}：不是可调参数")
            continue
        before = getattr(config, name)
        try:
            after = config.clamp_tunable(name, p["value"])
        except (TypeError, ValueError):
            applied.append(f"⚠ 跳过 {name}：数值 {p['value']!r} 无法解析")
            continue
        if after == before:
            continue
        clip = ""
        try:
            if config.TUNABLES[name]["cast"](p["value"]) != after:
                clip = "（提议越界，已夹到边界）"
            values[name] = after
            applied.append(f"{name}: {before} → {after}{clip}")
        except (TypeError, ValueError):
            continue

    if not values or not applied:
        return applied

    if config.TUNABLES_FILE.exists():
        shutil.copy2(config.TUNABLES_FILE, SNAPSHOT_FILE)
    else:
        SNAPSHOT_FILE.write_text(json.dumps({"values": {}, "history": []},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
    data["values"] = values
    data.setdefault("history", []).append({"day": day, "changes": applied})
    config.TUNABLES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return applied


def revert_params() -> str:
    if not SNAPSHOT_FILE.exists():
        return "没有可回滚的快照（还没应用过参数改动）。"
    shutil.copy2(SNAPSHOT_FILE, config.TUNABLES_FILE)
    return "已回滚到上一版参数。下次运行任何脚本时生效。"


def apply_playbook(ops: list[dict]) -> list[str]:
    """执行 playbook 的增/删/提权。"""
    if not ops:
        return []
    import selfreview
    path = prompts.PROMPTS_DIR / "playbook.md"
    text = path.read_text(encoding="utf-8")
    if pulse.PB_BEGIN not in text or pulse.PB_END not in text:
        return ["⚠ playbook.md 缺少 AUTO 标记，跳过"]
    head, rest = text.split(pulse.PB_BEGIN, 1)
    body, tail = rest.split(pulse.PB_END, 1)
    items = pulse.playbook_items(body)
    notes = []

    def find(frag: str) -> int:
        key = selfreview._norm(frag)[:12]
        for i, it in enumerate(items):
            if key and key in selfreview._norm(it):
                return i
        return -1

    for op in ops:
        content = op["content"]
        if op["op"] == "add":
            item = content if content.startswith("- ") else f"- {content}"
            if selfreview._is_dup(item, items):
                notes.append(f"跳过新增（和已有条目重复）：{content[:24]}…")
                continue
            items.insert(0, item)
            notes.append(f"新增方法：{content[:30]}…")
        elif op["op"] == "remove":
            i = find(content)
            if i < 0:
                notes.append(f"没找到要删的条目：{content[:24]}…")
                continue
            notes.append(f"删除方法：{items.pop(i)[2:32]}…")
        elif op["op"] == "promote":
            i = find(content)
            if i < 0:
                notes.append(f"没找到要提权的条目：{content[:24]}…")
                continue
            items.insert(0, items.pop(i))
            notes.append(f"提权到最前：{items[0][2:32]}…")

    if not notes:
        return []
    items = items[:PLAYBOOK_MAX]
    path.write_text(head + pulse.PB_BEGIN + "\n" + "\n".join(items) + "\n"
                    + pulse.PB_END + tail, encoding="utf-8")
    return notes


# ───────────────────────── 主流程 ─────────────────────────

def build_prompt(day: str) -> str:
    return prompts.load("optimize.md").format(
        day=day,
        winrate=winrate_text(),
        tunables=tunables_text(),
        playbook=pulse.load_playbook(limit=PLAYBOOK_MAX),
        reviews=recent_reviews(),
        vips=vips_text(),
        history=history_text(),
        min_n=MIN_SAMPLES,
    )


def _summary(report: str) -> str:
    out, capture = [], False
    for line in report.splitlines():
        if line.strip().startswith("## "):
            if capture:
                break
            capture = "摘要" in line
            continue
        if capture:
            out.append(line)
    return "\n".join(out).strip() or report[:800]


def run(model: str = DEFAULT_MODEL, dry_run: bool = False, post: bool = True,
        no_api: bool = False) -> str:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    prompt = build_prompt(day)

    if no_api:
        print(prompt[:5000] + f"\n…（prompt 共 {len(prompt)} 字）")
        return ""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("需要设置 ANTHROPIC_API_KEY 才能跑优化")

    from anthropic import Anthropic
    resp = Anthropic().messages.create(
        model=model, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    report = "\n".join(getattr(b, "text", "") for b in (resp.content or [])).strip()

    params = parse_params(report)
    pb_ops = parse_playbook_ops(report)
    print(f"AI 提出：{len(params)} 项参数调整、{len(pb_ops)} 项方法库调整")

    if dry_run:
        applied_p, applied_pb = ["（dry-run，未应用）"], []
    else:
        applied_p = apply_params(params, day)
        applied_pb = apply_playbook(pb_ops)

    OPT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OPT_DIR / f"{now.strftime('%Y%m%d')}-optimization.md"
    changelog = "\n".join(f"- {x}" for x in (applied_p + applied_pb)) or "- （本次没有实际改动）"
    out_path.write_text(
        f"# 系统自我优化 {day}\n\n"
        f"> 模型 {model}｜{'DRY-RUN 未应用' if dry_run else '已应用'}\n\n"
        f"## 实际生效的改动\n{changelog}\n\n"
        f"回滚参数：`python src/optimize.py --revert`\n\n---\n\n{report}\n",
        encoding="utf-8")
    print(f"决策记录已写入 {out_path}")
    for x in applied_p + applied_pb:
        print(f"  · {x}")

    if post:
        msg = (f"🔧 **系统自我优化** · {day}\n{pulse.tighten(_summary(report))}\n"
               f"**本次生效**\n{changelog}\n"
               f"_完整决策记录（含否决的替代方案）见 trade_notes/optimizations/{out_path.name}_")
        try:
            import discord_post
            discord_post.send(msg)
            print("摘要已推送到 Discord")
        except Exception as e:
            print(f"推送失败：{type(e).__name__}: {e}")

    return report


def show() -> None:
    print("当前可调参数：")
    for name, spec in config.TUNABLES.items():
        cur = getattr(config, name)
        default = config.tunable_defaults()[name]
        flag = "" if cur == default else f"  ← 已调整（默认 {default}）"
        print(f"  {name:<22} = {cur!s:<8} [{spec['min']}, {spec['max']}]{flag}")
    data = current_overrides()
    hist = data.get("history") or []
    if hist:
        print("\n调整历史：")
        for h in hist[-5:]:
            for c in h.get("changes", []):
                print(f"  {h.get('day')}  {c}")
    else:
        print("\n（还没有任何参数被调整过，全部是出厂默认）")


def main() -> None:
    ap = argparse.ArgumentParser(description="系统自我优化（调参数 + 改方法库 + 记录决策）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="默认用 opus，这步值得用好模型")
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不改任何东西")
    ap.add_argument("--no-post", action="store_true", help="不推送 Discord")
    ap.add_argument("--no-api", action="store_true", help="只打印 prompt（不花钱）")
    ap.add_argument("--revert", action="store_true", help="回滚到上一版参数")
    ap.add_argument("--show", action="store_true", help="看当前生效的参数")
    args = ap.parse_args()

    if args.revert:
        print(revert_params())
        return
    if args.show:
        show()
        return
    run(model=args.model, dry_run=args.dry_run, post=not args.no_post, no_api=args.no_api)


if __name__ == "__main__":
    main()
