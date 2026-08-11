"""把文本消息 POST 到 Discord 频道的 Webhook。

Webhook 是 Discord 官方功能：频道设置 → 整合 → Webhook → 新 Webhook → 复制 URL。
把 URL 放进项目根 .env 的 DISCORD_WEBHOOK_URL=...（.env 已 gitignore，别提交）。

用法（命令行自测）:
    python src/discord_post.py "hello from webhook"
    echo "多行内容" | python src/discord_post.py -

编程调用:
    import discord_post
    discord_post.send("**标题**\n正文...")

细节:
- Discord 单条消息上限 2000 字符 → 自动按行分片，尽量不切断 markdown。
- 命中 429 限流会读 retry_after 等待后重试。
- 没配 DISCORD_WEBHOOK_URL 时抛 RuntimeError（cycle.py 会据此跳过推送）。
"""

import argparse
import os
import sys
import time

import requests

import config  # noqa: F401  (load .env)

MAX_LEN = 1900          # 给 markdown/username 前缀留点余量，低于官方 2000 上限
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4


def webhook_url() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError(
            "未设置 DISCORD_WEBHOOK_URL。请在自己频道建一个 Webhook，"
            "把 URL 写进 .env 的 DISCORD_WEBHOOK_URL=..."
        )
    return url


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """按行把长文本切成 <= limit 的片段，尽量保留 markdown 行完整。"""
    text = text.rstrip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # 单行本身就超长 → 硬切
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        add = line if not cur else cur + "\n" + line
        if len(add) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = add
    if cur:
        chunks.append(cur)
    return chunks


def _post_once(url: str, content: str, username: str | None) -> None:
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if username:
        payload["username"] = username
    for attempt in range(MAX_RETRIES):
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 1.0))
            except (ValueError, KeyError, AttributeError):
                wait = 1.0
            time.sleep(min(wait + 0.25, 10))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Webhook 返回 {resp.status_code}: {resp.text[:300]}")
        return
    raise RuntimeError("Webhook 连续被 429 限流，放弃。")


def send(text: str, username: str | None = None) -> int:
    """把 text 推送到频道，超长自动分多条。返回实际发送的条数。"""
    url = webhook_url()
    parts = split_message(text)
    for i, part in enumerate(parts):
        _post_once(url, part, username)
        if i < len(parts) - 1:
            time.sleep(0.6)  # 分片之间轻微间隔，保持顺序、避免限流
    return len(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="把文本 POST 到 Discord Webhook")
    ap.add_argument("text", help="要发送的文本；用 - 表示从 stdin 读")
    ap.add_argument("--username", default=None, help="覆盖 Webhook 显示的用户名")
    args = ap.parse_args()
    text = sys.stdin.read() if args.text == "-" else args.text
    n = send(text, username=args.username)
    print(f"已发送 {n} 条。")


if __name__ == "__main__":
    main()
