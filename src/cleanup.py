"""分级磁盘清理：默认只报告，--apply 才执行删除/轮转。"""

import argparse
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import config

MB = 1024 * 1024
LARGE_INBOX_MB = 20
STATE_FILE_NAME = "_processed.json"


def _w(s: str) -> int:
    """字符串的显示宽度：中文/全角算 2 列，否则 1 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s: str, width: int, align: str = "<") -> str:
    """按**显示宽度**补空格（中文表格对齐的关键，str.ljust 按字符数会错位）。"""
    s = str(s)
    fill = max(0, width - _w(s))
    if align == ">":
        return " " * fill + s
    return s + " " * fill


@dataclass(frozen=True)
class Target:
    name: str
    path: Path
    keep_days: int
    note: str
    exclude_names: frozenset[str] = frozenset()


@dataclass
class TargetReport:
    target: Target
    exists: bool
    total_files: int = 0
    total_bytes: int = 0
    old_files: list[Path] | None = None
    old_bytes: int = 0


TARGETS = [
    Target("inbox/processed", config.INBOX_DIR / "processed",
           config.KEEP_INBOX_PROCESSED_DAYS, "原始导出备份",
           frozenset({STATE_FILE_NAME})),
    Target("market_cache", config.DATA_DIR / "market_cache",
           config.KEEP_MARKET_CACHE_DAYS, "按天行情缓存"),
    Target("cache/images", config.CACHE_DIR / "images",
           config.KEEP_CACHE_IMAGES_DAYS, "图片原图缓存"),
]

LOGS = [
    config.DATA_DIR / "cycle.log",
    config.INBOX_DIR / "watch.log",
]


def fmt_mb(size: int) -> str:
    return f"{size / MB:.1f} MB"


def iter_files(root: Path):
    if not root.exists() or not root.is_dir():
        return
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def build_report(target: Target, now: float) -> TargetReport:
    report = TargetReport(target=target, exists=target.path.exists(), old_files=[])
    if not target.path.exists() or not target.path.is_dir():
        return report

    cutoff = now - target.keep_days * 86400
    for p in iter_files(target.path):
        try:
            st = p.stat()
        except OSError:
            continue
        report.total_files += 1
        report.total_bytes += st.st_size
        if p.name in target.exclude_names:
            continue
        if st.st_mtime < cutoff:
            report.old_files.append(p)
            report.old_bytes += st.st_size
    return report


def large_inbox_files() -> list[tuple[Path, int]]:
    if not config.INBOX_DIR.exists():
        return []
    rows = []
    for p in config.INBOX_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > LARGE_INBOX_MB * MB:
            rows.append((p, size))
    return sorted(rows, key=lambda x: x[1], reverse=True)


def log_status(path: Path) -> tuple[bool, int]:
    if not path.exists():
        return False, 0
    try:
        size = path.stat().st_size
    except OSError:
        return False, 0
    return size > config.LOG_MAX_MB * MB, size


def rotate_log(path: Path) -> bool:
    need_rotate, _ = log_status(path)
    if not need_rotate:
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        backup = path.with_name(path.name + ".1")
        if backup.exists():
            backup.unlink()
        path.replace(backup)
        path.write_text("".join(lines[-config.LOG_KEEP_LINES:]), encoding="utf-8")
        return True
    except OSError as e:
        print(f"日志轮转失败：{path}：{e}")
        return False


def print_table(reports: list[TargetReport], apply: bool) -> None:
    title = "清理目标（即将执行）" if apply else "清理目标（试运行，不会删除）"
    print(f"{title}：")
    cols = [("目标", 18, "<"), ("保留", 6, ">"), ("文件数", 8, ">"),
            ("占用", 10, ">"), ("可清理", 8, ">"), ("可释放", 10, ">")]
    header = "  ".join(_pad(n, w, a) for n, w, a in cols) + "  说明"
    print(header)
    print("-" * _w(header))
    for r in reports:
        old_count = len(r.old_files or [])
        note = r.target.note + ("" if r.exists else "（目录不存在）")
        cells = [
            _pad(r.target.name, 18),
            _pad(f"{r.target.keep_days} 天", 6, ">"),
            _pad(r.total_files, 8, ">"),
            _pad(fmt_mb(r.total_bytes), 10, ">"),
            _pad(old_count, 8, ">"),
            _pad(fmt_mb(r.old_bytes), 10, ">"),
        ]
        print("  ".join(cells) + "  " + note)


def print_logs() -> None:
    print("\n日志轮转：")
    for p in LOGS:
        need_rotate, size = log_status(p)
        action = f"超过 {config.LOG_MAX_MB} MB，将保留最后 {config.LOG_KEEP_LINES} 行" if need_rotate else "无需轮转"
        if not p.exists():
            action = "文件不存在"
        print(f"- {p.relative_to(config.PROJECT_ROOT)}：{fmt_mb(size)}，{action}")


def print_warnings() -> None:
    rows = large_inbox_files()
    if not rows:
        return
    print(f"\n提醒：data\\inbox 根目录发现大文件（>{LARGE_INBOX_MB} MB），不会自动删除：")
    for p, size in rows:
        print(f"- {p.name}：{fmt_mb(size)}")
    print("  data\\inbox 可能也是 Chrome 下载目录，请人工确认后处理。")


def delete_old_files(reports: list[TargetReport]) -> tuple[int, int]:
    deleted = 0
    freed = 0
    for r in reports:
        for p in r.old_files or []:
            try:
                size = p.stat().st_size
                p.unlink()
                deleted += 1
                freed += size
            except OSError as e:
                print(f"删除失败：{p}：{e}")
    return deleted, freed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分级磁盘清理（默认只报告）")
    ap.add_argument("--apply", action="store_true", help="实际删除超期文件并轮转日志")
    ap.add_argument("--report", action="store_true", help="只打印报告（默认）")
    args = ap.parse_args(argv)

    reports = [build_report(t, time.time()) for t in TARGETS]
    print_table(reports, args.apply)
    print_logs()
    print_warnings()

    total_old = sum(len(r.old_files or []) for r in reports)
    total_old_bytes = sum(r.old_bytes for r in reports)
    if not args.apply:
        print(f"\n试运行完成：将清理 {total_old} 个文件，可释放 {fmt_mb(total_old_bytes)}。加 --apply 才会执行。")
        return 0

    deleted, freed = delete_old_files(reports)
    rotated = sum(1 for p in LOGS if rotate_log(p))
    print(f"\n清理完成：删除 {deleted} 个文件，释放 {fmt_mb(freed)}，轮转 {rotated} 个日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
