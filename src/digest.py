#!/usr/bin/env python3
"""
把整理好的聊天记录交给 Claude，输出按话题聚类的摘要 + 黑话注释。

用法:
    python digest.py discord-20260726.enriched.txt
    python digest.py in.txt -o digest.md --model claude-sonnet-5

超长输入会自动分块：先分别摘要，再合并成一份。
"""

import argparse
import json
import os
import sys
from pathlib import Path

import config
import prompts

DEFAULT_MODEL = "claude-sonnet-5"
CHUNK_CHARS = 120_000   # 单块字符上限，中文约等于 token 数量级
MAX_TOKENS = 8000
MAX_CONTINUATIONS = 4
CONTINUE_PROMPT = "继续，直接从上次中断处往下写，保持同样格式，不要重复已有内容。"

SYSTEM = prompts.load("digest_system.md")

PROMPT = prompts.load("digest_user.md")

MERGE_PROMPT = prompts.load("digest_merge.md")


def chunk(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and cur:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        out.append("".join(cur))
    return out


def extract_text(resp) -> str:
    """Extract text from Anthropic response across SDK versions/block shapes."""
    chunks: list[str] = []
    for b in getattr(resp, "content", []) or []:
        btype = getattr(b, "type", None)
        text = getattr(b, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
            continue
        # Some SDK shapes expose textual payload as an `input` string.
        if btype in {"input_text", "output_text"}:
            alt = getattr(b, "input", None)
            if isinstance(alt, str) and alt.strip():
                chunks.append(alt)
    return "\n".join(chunks).strip()


def write_debug(debug_path: Path | None, payload: dict) -> None:
    if not debug_path:
        return
    with debug_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def call(
    client,
    model: str,
    prompt: str,
    *,
    max_tokens: int,
    debug_path: Path | None = None,
    tag: str = "call",
) -> str:
    messages = [{"role": "user", "content": prompt}]
    chunks: list[str] = []
    last_stop_reason = None

    for attempt in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=messages,
        )
        part = extract_text(resp)
        stop_reason = getattr(resp, "stop_reason", None)
        last_stop_reason = stop_reason
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        block_types = [getattr(b, "type", "<unknown>") for b in getattr(resp, "content", []) or []]

        write_debug(
            debug_path,
            {
                "event": "api_response",
                "tag": tag,
                "attempt": attempt,
                "stop_reason": stop_reason,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "content_types": block_types,
                "extracted_chars": len(part),
            },
        )

        if not part.strip():
            raise RuntimeError(
                f"Claude 返回内容为空，无法生成摘要。stop_reason={stop_reason!r}, content_types={block_types}"
            )

        chunks.append(part.strip())

        if stop_reason != "max_tokens":
            break

        # On token truncation, ask Claude to continue from where it stopped.
        so_far = "\n\n".join(chunks)
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": so_far},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]

    if last_stop_reason == "max_tokens":
        raise RuntimeError(
            f"输出连续 {MAX_CONTINUATIONS + 1} 次触发 max_tokens，仍未结束。"
            f" 请提高 --max-tokens 或拆小输入。"
        )

    return "\n\n".join(chunks).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--debug", action="store_true", help="写出每次 API 调用的调试日志")
    ap.add_argument("--debug-file", type=Path, default=None)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("需要设置 ANTHROPIC_API_KEY")

    from anthropic import Anthropic
    client = Anthropic()

    out = args.output or args.input.with_suffix(".digest.md")
    debug_path = None
    if args.debug:
        debug_path = args.debug_file or out.with_suffix(".digest.debug.jsonl")
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text("", encoding="utf-8")
        write_debug(
            debug_path,
            {
                "event": "run_start",
                "input": str(args.input),
                "output": str(out),
                "model": args.model,
                "max_tokens": args.max_tokens,
            },
        )

    text = args.input.read_text(encoding="utf-8")
    parts = chunk(text, CHUNK_CHARS)

    if len(parts) == 1:
        result = call(
            client,
            args.model,
            PROMPT.format(content=parts[0]),
            max_tokens=args.max_tokens,
            debug_path=debug_path,
            tag="single",
        )
    else:
        print(f"输入较长，分 {len(parts)} 段处理")
        partials = []
        for i, p in enumerate(parts, 1):
            print(f"  处理第 {i}/{len(parts)} 段…")
            partials.append(
                call(
                    client,
                    args.model,
                    PROMPT.format(content=p),
                    max_tokens=args.max_tokens,
                    debug_path=debug_path,
                    tag=f"chunk_{i}",
                )
            )
        print("  合并…")
        joined = "\n\n---\n\n".join(partials)
        result = call(
            client,
            args.model,
            MERGE_PROMPT.format(content=joined),
            max_tokens=args.max_tokens,
            debug_path=debug_path,
            tag="merge",
        )

    if not result.strip():
        sys.exit("生成结果为空，已停止写文件。请检查 API 响应与网络/权限。")

    out.write_text(result, encoding="utf-8")
    print(f"完成 -> {out}")
    if debug_path:
        print(f"调试日志 -> {debug_path}")


if __name__ == "__main__":
    main()
