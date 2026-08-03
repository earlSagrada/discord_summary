#!/usr/bin/env python3
"""
把 Discord 导出文本里的 [IMG#N] 占位符替换成图片的文字转写。

流程：解析图片清单 -> 下载（按 URL 路径缓存）-> 缩放 -> 调 Claude 转写
（按图片内容 sha256 缓存）-> 回填到正文。

用法:
    python enrich_images.py discord-20260726.txt
    python enrich_images.py discord-20260726.txt -o out.txt --model claude-haiku-4-5-20251001

没有设置 ANTHROPIC_API_KEY 时脚本仍可运行，只是不做转写，
占位符会保留成 [图片#N 未转写: <url>]。
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

# ───────────────────────────── 配置 ─────────────────────────────

MANIFEST_HEADER = "===== 图片清单 ====="
MANIFEST_LINE = re.compile(r"^IMG#(\d+)\s+(\S+)")
PLACEHOLDER = re.compile(r"\[IMG#(\d+)\]")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_EDGE = 1400          # 长边缩放上限，够看清截图里的中文
JPEG_QUALITY = 85
MAX_BYTES = 4_500_000    # API 单张图片上限留点余量
REQUEST_TIMEOUT = 30
RETRIES = 3

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

TRANSCRIBE_PROMPT = """你在帮一个交易讨论群做聊天记录整理。下面是群里有人发的一张图片。

请按图片类型处理：

1. 文字截图（推特、新闻、聊天记录、研报片段等）——逐字提取里面的文字内容，
   保留发言人/账号名。不要翻译，不要总结，不要加评论。
2. 图表（K线、指标走势、持仓表等）——用 1-3 句话说明：图表标的和名称、
   时间范围、当前读数或最新位置、明显的趋势或异常。不要逐点描述。
3. 表情包 / 与交易无关的图——只输出一行 `[无关图片]`。

直接输出结果，不要任何前言、不要 markdown 代码块。控制在 250 字以内。"""


# ───────────────────────────── 缓存 ─────────────────────────────

class Cache:
    def __init__(self, root: Path):
        self.root = root
        self.img_dir = root / "images"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.url_index_path = root / "url_index.json"   # url_key -> content_hash
        self.transcripts_path = root / "transcripts.json"  # content_hash -> text
        self.url_index = self._load(self.url_index_path)
        self.transcripts = self._load(self.transcripts_path)

    @staticmethod
    def _load(p: Path) -> dict:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {}

    def save(self):
        self.url_index_path.write_text(
            json.dumps(self.url_index, ensure_ascii=False, indent=1), encoding="utf-8")
        self.transcripts_path.write_text(
            json.dumps(self.transcripts, ensure_ascii=False, indent=1), encoding="utf-8")


def url_key(url: str) -> str:
    """Discord CDN 的签名参数每次刷新都会变，所以只对路径做 key（附件 ID 是稳定的）。"""
    return hashlib.sha256(urlparse(url).path.encode()).hexdigest()[:24]


# ─────────────────────────── 下载与预处理 ───────────────────────────

def is_image_url(url: str) -> bool:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext in IMAGE_EXTS


def download(url: str) -> bytes | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.content
            if r.status_code in (403, 404):
                # 签名过期或附件已删，重试没有意义
                print(f"    HTTP {r.status_code}（链接可能已过期）", file=sys.stderr)
                return None
            print(f"    HTTP {r.status_code}，重试中…", file=sys.stderr)
        except requests.RequestException as e:
            print(f"    下载出错 {e}，重试中…", file=sys.stderr)
        time.sleep(1.5 * (attempt + 1))
    return None


def shrink(raw: bytes) -> tuple[bytes, str] | None:
    """缩放并转成 JPEG，控制 token 消耗。返回 (bytes, media_type)。"""
    try:
        im = Image.open(io.BytesIO(raw))
        im.seek(0)  # GIF 取第一帧
    except Exception as e:
        print(f"    无法解析图片: {e}", file=sys.stderr)
        return None

    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")

    if max(im.size) > MAX_EDGE:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    data = buf.getvalue()
    if len(data) > MAX_BYTES:
        buf = io.BytesIO()
        im.thumbnail((900, 900), Image.LANCZOS)
        im.save(buf, format="JPEG", quality=75, optimize=True)
        data = buf.getvalue()
    return data, "image/jpeg"


# ───────────────────────────── 转写 ─────────────────────────────

def transcribe(client, model: str, data: bytes, media_type: str) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(data).decode(),
                }},
                {"type": "text", "text": TRANSCRIBE_PROMPT},
            ],
        }],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ───────────────────────────── 主流程 ─────────────────────────────

def parse_manifest(text: str) -> tuple[str, dict[int, str]]:
    if MANIFEST_HEADER not in text:
        return text, {}
    body, _, manifest = text.partition(MANIFEST_HEADER)
    urls = {}
    for line in manifest.splitlines():
        m = MANIFEST_LINE.match(line.strip())
        if m:
            urls[int(m.group(1))] = m.group(2)
    return body.rstrip(), urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="脚本导出的 .txt")
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=Path("cache"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-api", action="store_true", help="只下载和缓存，不调用 API")
    args = ap.parse_args()

    text = args.input.read_text(encoding="utf-8")
    body, urls = parse_manifest(text)
    if not urls:
        print("没有找到图片清单，原样输出。")
        out = args.output or args.input.with_suffix(".enriched.txt")
        out.write_text(body, encoding="utf-8")
        return

    cache = Cache(args.cache)

    client = None
    if not args.no_api and os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic
        client = Anthropic()
    elif not args.no_api:
        print("未设置 ANTHROPIC_API_KEY，跳过转写。", file=sys.stderr)

    results: dict[int, str] = {}
    hit = miss = 0

    for idx in sorted(urls):
        url = urls[idx]
        print(f"[{idx}/{len(urls)}] {url[:90]}")

        if not is_image_url(url):
            results[idx] = f"[附件 {Path(urlparse(url).path).name}]"
            continue

        ukey = url_key(url)
        chash = cache.url_index.get(ukey)
        path = cache.img_dir / f"{chash}.jpg" if chash else None

        if not (path and path.exists()):
            raw = download(url)
            if raw is None:
                results[idx] = f"[图片下载失败: {url}]"
                continue
            shrunk = shrink(raw)
            if shrunk is None:
                results[idx] = "[图片无法解析]"
                continue
            data, _ = shrunk
            chash = hashlib.sha256(data).hexdigest()[:24]
            path = cache.img_dir / f"{chash}.jpg"
            path.write_bytes(data)
            cache.url_index[ukey] = chash
        else:
            data = path.read_bytes()

        if chash in cache.transcripts:
            results[idx] = cache.transcripts[chash]
            hit += 1
            print("    命中缓存")
            continue

        if client is None:
            results[idx] = f"未转写: {url}"
            continue

        try:
            txt = transcribe(client, args.model, data, "image/jpeg")
            cache.transcripts[chash] = txt
            cache.save()
            results[idx] = txt
            miss += 1
            print(f"    转写完成 ({len(txt)} 字)")
        except Exception as e:
            print(f"    转写失败: {e}", file=sys.stderr)
            results[idx] = f"[转写失败: {url}]"

    cache.save()

    def sub(m):
        i = int(m.group(1))
        val = results.get(i, "").replace("\n", "\n     ")
        return f"[图片#{i}: {val}]" if val else m.group(0)

    enriched = PLACEHOLDER.sub(sub, body)
    out = args.output or args.input.with_suffix(".enriched.txt")
    out.write_text(enriched, encoding="utf-8")
    print(f"\n完成 -> {out}    新转写 {miss} 张，缓存命中 {hit} 张")


if __name__ == "__main__":
    main()
