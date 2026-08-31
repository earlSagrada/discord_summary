"""读 Frank 的 Substack 复盘 PDF（data/substack_post/*.pdf），抽成纯文本供复盘学习。

为什么单独做一个模块：这些 PDF 是**方法论的最好样本**——它示范了一条完整的
分析链路：宏观因果链（第一层…第七层）→ GEX/DEX 关键位 → 情景路径 → 明确的
建仓/加仓/止损/目标。我们的 pulse 想变强，就该学这套结构，而不是自己瞎编。

实现说明：
- 用 PyMuPDF 直接取文字层。**注意**：不同导出方式的 PDF 差别很大，早期一版
  用的字体没有 ToUnicode 映射，取出来的文字缺了所有英文和数字（"10Y 收 4.7%"
  变成 "收"），完全没法用。所以 `extract()` 会做一次质量检查，数字/英文占比
  太低就直接报错，而不是把残缺文本喂给 AI。
- 抽出的文本缓存成同名 .txt，避免每次复盘都重新解析。

用法：
    python src/substack.py                 # 解析全部，报告状态
    python src/substack.py --force         # 忽略缓存重新解析
    python src/substack.py --latest 2      # 只看最近 2 篇的摘要
"""

import argparse
import json
import re
from pathlib import Path

import config  # noqa: F401  (UTF-8 stdout + load .env)

POSTS_DIR = config.DATA_DIR / "substack_post"
# 正文里数字+英文的最低占比，低于此说明 PDF 字体没有 ToUnicode 映射，文本残缺
MIN_ALNUM_RATIO = 0.05
MIN_CHARS = 500


class ExtractError(RuntimeError):
    pass


def _quality(text: str) -> float:
    if not text:
        return 0.0
    return len(re.findall(r"[0-9A-Za-z]", text)) / len(text)


def _clean(text: str) -> str:
    """去掉 Substack 页脚/页码这类每页重复的噪音。"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "substack.com/p/" in line or line.startswith("https://"):
            continue
        if re.fullmatch(r"\d+\s+of\s+\d+", line):
            continue
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4},?\s+\d{1,2}:\d{2}\s*[ap]m", line, re.I):
            continue
        out.append(line)
    return "\n".join(out)


def extract(pdf: Path, force: bool = False) -> str:
    """PDF → 纯文本（带缓存）。文本质量不合格会抛 ExtractError。"""
    cache = pdf.with_suffix(".txt")
    if cache.exists() and not force:
        cached = cache.read_text(encoding="utf-8")
        if len(cached) >= MIN_CHARS:
            return cached

    try:
        import pymupdf
    except ImportError as e:
        raise ExtractError("需要 pymupdf：pip install pymupdf") from e

    with pymupdf.open(pdf) as doc:
        raw = "\n".join(page.get_text() for page in doc)

    text = _clean(raw)
    if len(text) < MIN_CHARS:
        raise ExtractError(f"{pdf.name}：只取到 {len(text)} 字，PDF 可能是扫描件")
    ratio = _quality(text)
    if ratio < MIN_ALNUM_RATIO:
        raise ExtractError(
            f"{pdf.name}：文本里英文/数字只占 {ratio:.1%}，说明 PDF 字体缺 ToUnicode 映射，"
            f"所有价位和代码都丢了。请换一种方式导出 PDF（例如浏览器打印为 PDF）。")

    cache.write_text(text, encoding="utf-8")
    return text


def list_posts() -> list[Path]:
    if not POSTS_DIR.exists():
        return []
    return sorted(POSTS_DIR.glob("*.pdf"))


# ───────────────────── 新文件检测（自动触发复盘+优化用）─────────────────────

SEEN_FILE = config.DATA_DIR / "substack_seen.json"


def _load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return set()


def new_posts() -> list[Path]:
    """还没处理过的 PDF。**只读不写**，调用方确认处理成功后再 mark_seen()。

    分开读写是有意的：如果解析或复盘中途失败了，下一轮还会重试，
    不会因为"标记过了"就把一期内容永久漏掉。
    """
    seen = _load_seen()
    return [p for p in list_posts() if p.name not in seen]


def mark_seen(paths: list[Path]) -> None:
    seen = _load_seen() | {p.name for p in paths}
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2),
                         encoding="utf-8")


def mark_all_seen() -> int:
    """把现有 PDF 全标成已处理（首次启用时用，避免一次性触发一堆历史文件）。"""
    posts = list_posts()
    mark_seen(posts)
    return len(posts)


def load_recent(limit: int = 2, max_chars: int = 12000) -> tuple[str, list[str]]:
    """取最近 limit 篇的正文，拼成给复盘用的一段。返回 (文本, 用到的篇名)。"""
    used, chunks = [], []
    for pdf in list_posts()[-limit:]:
        try:
            body = extract(pdf)
        except ExtractError as e:
            print(f"跳过 {pdf.name}：{e}")
            continue
        used.append(pdf.stem)
        chunks.append(f"### 《{pdf.stem}》Frank 复盘与展望\n{body}")
    text = "\n\n".join(chunks)
    return text[:max_chars], used


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 Substack 复盘 PDF")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新解析")
    ap.add_argument("--latest", type=int, default=0, help="只打印最近 N 篇的开头")
    ap.add_argument("--check-new", action="store_true", help="列出还没触发过复盘的 PDF")
    ap.add_argument("--mark-all-seen", action="store_true",
                    help="把现有 PDF 全标成已处理（首次启用自动触发时用）")
    args = ap.parse_args()

    if args.mark_all_seen:
        print(f"已把 {mark_all_seen()} 个 PDF 标记为已处理，之后只有新增文件才会触发复盘。")
        return
    if args.check_new:
        fresh = new_posts()
        print("\n".join(p.name for p in fresh) if fresh else "（没有新 PDF）")
        return

    posts = list_posts()
    if not posts:
        print(f"{POSTS_DIR} 下没有 PDF。把 Frank 的复盘导出成 PDF 放进去即可。")
        return
    for pdf in posts:
        try:
            text = extract(pdf, force=args.force)
            print(f"✅ {pdf.name}：{len(text)} 字，英文数字占比 {_quality(text):.1%} "
                  f"→ {pdf.with_suffix('.txt').name}")
        except ExtractError as e:
            print(f"❌ {e}")
    if args.latest:
        body, used = load_recent(args.latest)
        print(f"\n最近 {len(used)} 篇（{', '.join(used)}）开头：\n{body[:600]}…")


if __name__ == "__main__":
    main()
