#!/usr/bin/env python3
"""
盯盘 MVP v0 · 本地入库 watcher

监听 inbox/ 里油猴导出脚本每 15 分钟丢进来的 discord-*.json，
按 message id 去重合并到「每天一份」的记录，再自动调 enrich_images.py
把图片转写补上。**不跑 digest**（digest 手动/每日批量，省 API 钱）。

产物（写到 chats_by_date/<YYYYMMDD>/）：
    merged.json          去重合并后的原始记录（id 唯一，按时间排序）
    merged.txt           压缩文本（格式同油猴脚本，enrich/digest 直接吃）
    merged.enriched.txt  图片转写回填后的文本 —— 每日 digest 就跑这个

用法:
    # 常驻模式：一直盯着 inbox/
    python watch_inbox.py

    # 跑一遍现有积压就退出（可挂到 Windows 计划任务，每 15min 触发一次）
    python watch_inbox.py --once

去重逻辑对齐油猴脚本 harvest()：同一条消息保留信息更全的那份
（有作者 / 图片更多 / 正文更长者胜）。enrich_images.py 自带 sha256 图片缓存，
所以每天反复 enrich 只有「新图片」才花钱，旧图片命中缓存。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import config

ROOT = config.PROJECT_ROOT
INBOX = config.INBOX_DIR
PROCESSED_DIR = INBOX / "processed"
OUT_ROOT = config.CHATS_DIR
CACHE_DIR = config.CACHE_DIR
ENRICH = config.SRC_DIR / "enrich_images.py"
STATE_FILE = PROCESSED_DIR / "_processed.json"
LOG_FILE = INBOX / "watch.log"

POLL_SEC = 20        # 常驻模式轮询间隔
STABLE_SEC = 3       # 文件 mtime 至少这么旧才算写完（避免读到半个下载）

MANIFEST_HEADER = "===== 图片清单 ====="


# ───────────────────────────── 小工具 ─────────────────────────────

def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_state(done: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=1),
                          encoding="utf-8")


def is_stable(p: Path) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) >= STABLE_SEC
    except OSError:
        return False


# ─────────────────────── 去重合并 & 文本生成 ───────────────────────

def more_complete(new: dict, old: dict) -> bool:
    """new 是否比 old 信息更全（对齐油猴 harvest 的取舍）。"""
    if (not old.get("author")) and new.get("author"):
        return True
    if len(new.get("media") or []) > len(old.get("media") or []):
        return True
    if len(new.get("text") or "") > len(old.get("text") or ""):
        return True
    return False


def merge_records(existing: dict[str, dict], new_records: list[dict]) -> int:
    """把 new_records 合并进 existing(id->record)，返回新增/更新条数。"""
    changed = 0
    for r in new_records:
        rid = r.get("id")
        if not rid or not r.get("ts"):
            continue
        prev = existing.get(rid)
        if prev is None:
            existing[rid] = r
            changed += 1
        elif more_complete(r, prev):
            merged = {**prev, **r}
            merged["author"] = r.get("author") or prev.get("author")
            # 保留图片更多的那份
            if len(prev.get("media") or []) > len(r.get("media") or []):
                merged["media"] = prev["media"]
            existing[rid] = merged
            changed += 1
    return changed


def to_compact_text(records: list[dict]) -> str:
    """复刻油猴脚本 toCompactText 的输出格式，供 enrich_images.py 消费。"""
    rows = sorted((r for r in records if r.get("ts")), key=lambda r: r["ts"])
    # 分组消息（连发无 header）向前填充作者
    last = None
    for r in rows:
        if r.get("author"):
            last = r["author"]
        else:
            r["author"] = last or "(unknown)"

    imgs: list[str] = []

    def idx_of(u: str) -> int:
        if u in imgs:
            return imgs.index(u) + 1
        imgs.append(u)
        return len(imgs)

    lines: list[str] = []
    last_day = ""
    for r in rows:
        ts = r["ts"]                 # ISO, e.g. 2026-07-27T18:44:12.345Z
        day = ts[:10]
        hm = ts[11:16]
        if day != last_day:
            lines.append(f"\n===== {day} =====")
            last_day = day
        lines.append(f"{hm} {r['author']}")
        rep = r.get("reply")
        if rep:
            lines.append(f"  ↩ 回复 {rep.get('author', '')}:「{rep.get('text', '')}」")
        for l in (r.get("text") or "").split("\n"):
            if l.strip():
                lines.append(f"  {l.strip()}")
        for e in (r.get("embeds") or []):
            parts = " / ".join(x for x in (e.get("author"), e.get("title"), e.get("desc")) if x)
            if parts:
                lines.append(f"  [EMBED] {parts[:400]}")
        for m in (r.get("media") or []):
            lines.append(f"  [IMG#{idx_of(m)}]")

    lines.append(f"\n{MANIFEST_HEADER}")
    for i, u in enumerate(imgs):
        lines.append(f"IMG#{i + 1}\t{u}")
    return "\n".join(lines)


def days_in(records: list[dict]) -> set[str]:
    return {r["ts"][:10] for r in records if r.get("ts")}


# ───────────────────────────── 频道解析 ─────────────────────────────

# 多频道导出文件名：discord-<频道名>-<channelId>-<时间戳>.json
#   时间戳来自 JS 的 toISOString().slice(0,16) 去掉 T/: → 形如 2026-08-111927（含日期横线）
# 旧单频道格式：       discord-<时间戳>.json（无频道标签）
_TAGGED = re.compile(r"^discord-(?P<label>.+?)-(?P<chid>\d{15,25})-(?P<stamp>[\dT:\-]+)$")
_UNTAGGED = re.compile(r"^discord-(?P<stamp>[\dT:\-]+)$")
_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


def channel_of(json_path: Path) -> str:
    """从导出文件名解析频道名。旧格式/解析不了统一归到 'misc'。"""
    stem = json_path.stem
    m = _TAGGED.match(stem)
    if m:
        label = m.group("label").strip() or "misc"
        return _SAFE.sub("_", label) or "misc"
    return "misc"


# ───────────────────────────── 处理一个导出 ─────────────────────────────

def process_export(json_path: Path) -> None:
    try:
        records = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"跳过（读不了 {json_path.name}）: {e}")
        return
    if not isinstance(records, list) or not records:
        log(f"跳过（{json_path.name} 无记录）")
        return

    channel = channel_of(json_path)

    for day in sorted(days_in(records)):
        folder = OUT_ROOT / day.replace("-", "") / channel
        folder.mkdir(parents=True, exist_ok=True)
        merged_json = folder / "merged.json"
        merged_txt = folder / "merged.txt"
        merged_enriched = folder / "merged.enriched.txt"

        existing: dict[str, dict] = {}
        if merged_json.exists():
            try:
                for r in json.loads(merged_json.read_text(encoding="utf-8")):
                    if r.get("id"):
                        existing[r["id"]] = r
            except (json.JSONDecodeError, OSError):
                pass

        day_records = [r for r in records if r.get("ts", "")[:10] == day]
        changed = merge_records(existing, day_records)
        if changed == 0 and merged_txt.exists():
            log(f"{day}/{channel}: 无新增，跳过 enrich")
            continue

        ordered = sorted(existing.values(), key=lambda r: r.get("ts") or "")
        merged_json.write_text(json.dumps(ordered, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        merged_txt.write_text(to_compact_text(ordered), encoding="utf-8")
        log(f"{day}/{channel}: 合并后 {len(ordered)} 条（本次 +{changed}），开始 enrich")

        rc = subprocess.run(
            [sys.executable, str(ENRICH), str(merged_txt),
             "-o", str(merged_enriched), "--cache", str(CACHE_DIR)],
            cwd=str(ROOT),
        ).returncode
        if rc == 0:
            log(f"{day}/{channel}: enrich 完成 -> {merged_enriched.relative_to(ROOT)}")
        else:
            log(f"{day}/{channel}: enrich 退出码 {rc}（图片转写可能不全，文本仍已更新）")


def archive(json_path: Path) -> None:
    """把这次导出的 .json/.txt/-images.txt 挪到 processed/，保持 inbox 干净。"""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    stem = json_path.stem  # discord-<stamp>
    for sib in (json_path,
                json_path.with_suffix(".txt"),
                json_path.with_name(stem + "-images.txt")):
        if sib.exists():
            try:
                shutil.move(str(sib), str(PROCESSED_DIR / sib.name))
            except OSError as e:
                log(f"归档失败 {sib.name}: {e}")


def scan_once(done: set[str]) -> int:
    n = 0
    for json_path in sorted(INBOX.glob("discord-*.json")):
        if json_path.name in done or not is_stable(json_path):
            continue
        log(f"处理 {json_path.name}")
        process_export(json_path)
        archive(json_path)
        done.add(json_path.name)
        save_state(done)
        n += 1
    return n


# ───────────────────────────── main ─────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="盯盘 MVP v0 本地入库 watcher")
    ap.add_argument("--once", action="store_true",
                    help="处理完现有积压就退出（适合挂 Windows 计划任务）")
    ap.add_argument("--poll", type=int, default=POLL_SEC,
                    help=f"常驻模式轮询间隔秒数（默认 {POLL_SEC}）")
    args = ap.parse_args()

    INBOX.mkdir(parents=True, exist_ok=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    done = load_state()
    log(f"watcher 启动 · inbox={INBOX} · 已处理 {len(done)} 个导出 · "
        f"{'单次' if args.once else '常驻'}模式")

    if args.once:
        n = scan_once(done)
        log(f"单次处理完成，本次 {n} 个新导出。")
        return

    try:
        while True:
            scan_once(done)
            time.sleep(max(5, args.poll))
    except KeyboardInterrupt:
        log("收到中断，watcher 退出。")


if __name__ == "__main__":
    main()
