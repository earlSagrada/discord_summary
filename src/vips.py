"""VIP（重点发言人）名单管理：把复盘提出的建议变成实际改动。

背景：`review.py` / `selfreview.py` 会把"建议加谁、减谁"写进
`prompts/vip_suggestions.md`，但**不会自动改** `vip_speakers.txt`——
因为这份名单决定 pulse 重点关注谁，影响很大。

这个模块把"人工确认"这一步做成可操作的流程：

    python src/vips.py              # 看现在名单 + 有哪些待确认建议
    python src/vips.py --apply      # 全部采纳（加 + 减）
    python src/vips.py --add 张三 --remove 李四
    python src/vips.py --auto       # 自动模式：只采纳"连续 2 期都被建议"的
    python src/vips.py --undo       # 撤回上一次改动

**自动模式的门槛**：同一个人必须在**连续 2 期复盘**里都被建议，才会真的落地。
单期建议往往来自一两句话的印象，两期都提说明是稳定观察。
计数状态存在 `data/vip_state.json`，采纳后清零。

每次改动都会备份上一版到 `data/vip_speakers.bak`，`--undo` 可原样还原。
"""

import argparse
import json
import re
import shutil
from datetime import datetime, timezone

import config
import prompts

VIP_FILE = prompts.PROMPTS_DIR / "vip_speakers.txt"
SUGGEST_FILE = prompts.PROMPTS_DIR / "vip_suggestions.md"
STATE_FILE = config.DATA_DIR / "vip_state.json"
BACKUP_FILE = config.DATA_DIR / "vip_speakers.bak"
# 自动模式下，一个建议要连续出现几期才落地
AUTO_STREAK = 2

_ADD_HEADS = ("新增", "加入", "建议加")
_DEL_HEADS = ("移除", "移出", "删除", "降级", "噪音")
# 这些词出现在标题里就**整块跳过**。必须比 _ADD_HEADS 先判断，
# 否则「建议观察（暂不加入）」会因为含"加入"被误当成建议加入——
# 而复盘的原意恰恰是"先别加"。
_SKIP_HEADS = ("观察", "暂不", "待定", "无需", "维持", "保持", "保留", "不调整")


# ───────────────────────── 名单读写 ─────────────────────────

def load_list() -> tuple[list[str], list[str]]:
    """返回 (注释行, 名字列表)，改写时保留原有注释头。"""
    if not VIP_FILE.exists():
        return [], []
    header, names = [], []
    for raw in VIP_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if not names:  # 只保留名字出现之前的注释头
                header.append(raw.rstrip())
            continue
        names.append(line)
    return header, names


def save_list(header: list[str], names: list[str]) -> None:
    if VIP_FILE.exists():
        shutil.copy2(VIP_FILE, BACKUP_FILE)
    body = "\n".join(header + names)
    VIP_FILE.write_text(body.rstrip() + "\n", encoding="utf-8")


def undo() -> bool:
    if not BACKUP_FILE.exists():
        return False
    shutil.copy2(BACKUP_FILE, VIP_FILE)
    return True


# ───────────────────────── 解析建议 ─────────────────────────

def _names_in(block: str) -> list[str]:
    """从一段 Markdown 里抠出人名：`- **名字**：理由` 或 `- 名字：理由`。"""
    found = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("-").strip()
        m = re.match(r"\*\*(.+?)\*\*", line)
        name = m.group(1) if m else re.split(r"[：:（(]", line)[0]
        name = name.strip().strip("*`").strip()
        if name and len(name) <= 40 and "无" != name:
            found.append(name)
    return found


def parse_suggestions() -> dict[str, list[str]]:
    """把 vip_suggestions.md 解析成 {'add': [...], 'remove': [...]}。

    按 `###` 小标题分块，标题里含"新增/加入"归 add，含"移除/降级"归 remove。
    """
    out = {"add": [], "remove": []}
    if not SUGGEST_FILE.exists():
        return out
    text = SUGGEST_FILE.read_text(encoding="utf-8")
    blocks: list[tuple[str, list[str]]] = []
    cur_head, cur_lines = "", []
    for line in text.splitlines():
        if line.strip().startswith("###"):
            blocks.append((cur_head, cur_lines))
            cur_head, cur_lines = line.strip("# ").strip(), []
        else:
            cur_lines.append(line)
    blocks.append((cur_head, cur_lines))

    for head, lines in blocks:
        if not head:
            continue
        block = "\n".join(lines)
        # 顺序要紧：先跳过"观察/暂不/维持"这类，再判断移除，最后才是新增。
        if any(k in head for k in _SKIP_HEADS):
            continue
        if any(k in head for k in _DEL_HEADS):
            out["remove"] += _names_in(block)
        elif any(k in head for k in _ADD_HEADS):
            out["add"] += _names_in(block)
    # 同一个名字同时出现在两边时以移除为准（更保守）
    out["add"] = [n for n in dict.fromkeys(out["add"]) if n not in out["remove"]]
    out["remove"] = list(dict.fromkeys(out["remove"]))
    return out


# ───────────────────────── 连续期数计数（自动模式）─────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"streak": {}, "last_seen": ""}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def bump_streaks(sug: dict[str, list[str]]) -> dict[str, int]:
    """把本期建议计入连续计数；本期没再提的清零。返回 {'add:名字': 次数}。

    只统计**真正需要动手**的建议：已经在名单里的人再被"建议加入"、
    本来就不在名单里的人被"建议移除"，都是空操作，不该占计数。
    """
    _, names = load_list()
    lower = {n.lower() for n in names}
    state = _load_state()
    streak: dict[str, int] = state.get("streak", {})
    current = ({f"add:{n}" for n in sug["add"] if n.lower() not in lower}
               | {f"remove:{n}" for n in sug["remove"] if n.lower() in lower})
    new_streak = {k: streak.get(k, 0) + 1 for k in current}
    state["streak"] = new_streak
    state["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_state(state)
    return new_streak


def clear_streaks(keys: list[str]) -> None:
    state = _load_state()
    for k in keys:
        state.get("streak", {}).pop(k, None)
    _save_state(state)


# ───────────────────────── 应用 ─────────────────────────

def apply(add: list[str], remove: list[str]) -> tuple[list[str], list[str]]:
    """真正改名单。返回 (实际加了谁, 实际删了谁)。"""
    header, names = load_list()
    lower = {n.lower(): n for n in names}
    added = [n for n in add if n.lower() not in lower]
    removed = [lower[n.lower()] for n in remove if n.lower() in lower]
    if not added and not removed:
        return [], []
    kept = [n for n in names if n not in removed] + added
    save_list(header, kept)
    return added, removed


def run_auto(streak_min: int = AUTO_STREAK) -> tuple[list[str], list[str]]:
    """自动模式：把连续出现够 streak_min 期的建议落地。"""
    sug = parse_suggestions()
    streak = bump_streaks(sug)
    ripe_add = [n for n in sug["add"] if streak.get(f"add:{n}", 0) >= streak_min]
    ripe_del = [n for n in sug["remove"] if streak.get(f"remove:{n}", 0) >= streak_min]
    added, removed = apply(ripe_add, ripe_del)
    clear_streaks([f"add:{n}" for n in added] + [f"remove:{n}" for n in removed])
    return added, removed


# ───────────────────────── CLI ─────────────────────────

def _report() -> None:
    _, names = load_list()
    print(f"当前重点发言人（{len(names)} 位）：")
    for n in names:
        print(f"  · {n}")
    sug = parse_suggestions()
    if not SUGGEST_FILE.exists():
        print("\n还没有复盘建议（prompts/vip_suggestions.md 不存在）。")
        return
    streak = _load_state().get("streak", {})
    print("\n待确认的建议：")
    if not sug["add"] and not sug["remove"]:
        print("  （本期无增删建议）")
    for n in sug["add"]:
        mark = "✅ 已在名单里" if n in names else f"连续 {streak.get(f'add:{n}', 0)} 期建议"
        print(f"  ＋ 加入 {n}（{mark}）")
    for n in sug["remove"]:
        mark = "已经不在名单里" if n not in names else f"连续 {streak.get(f'remove:{n}', 0)} 期建议"
        print(f"  － 移除 {n}（{mark}）")
    print(f"\n采纳全部：python src/vips.py --apply"
          f"\n只采纳连续 {AUTO_STREAK} 期的：python src/vips.py --auto"
          f"\n改错了：python src/vips.py --undo")


def main() -> None:
    ap = argparse.ArgumentParser(description="重点发言人名单管理（确认复盘提出的增删建议）")
    ap.add_argument("--apply", action="store_true", help="采纳 vip_suggestions.md 里的全部建议")
    ap.add_argument("--auto", action="store_true",
                    help=f"只采纳连续 {AUTO_STREAK} 期都被建议的（自动化流程用）")
    ap.add_argument("--add", action="append", default=[], help="手动加一个人（可重复）")
    ap.add_argument("--remove", action="append", default=[], help="手动删一个人（可重复）")
    ap.add_argument("--undo", action="store_true", help="撤回上一次改动")
    args = ap.parse_args()

    if args.undo:
        print("已撤回到上一版名单。" if undo() else "没有备份可撤回。")
        return

    if args.auto:
        added, removed = run_auto()
    elif args.apply or args.add or args.remove:
        sug = parse_suggestions() if args.apply else {"add": [], "remove": []}
        added, removed = apply(sug["add"] + args.add, sug["remove"] + args.remove)
    else:
        _report()
        return

    if added or removed:
        if added:
            print(f"已加入：{'、'.join(added)}")
        if removed:
            print(f"已移除：{'、'.join(removed)}")
        print(f"（改错了可以 python src/vips.py --undo 撤回）")
    else:
        print("没有需要改动的（建议要么已生效，要么还没到自动落地的门槛）。")
    _report()


if __name__ == "__main__":
    main()
