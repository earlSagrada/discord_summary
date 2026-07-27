#!/usr/bin/env python3
"""
把整理好的聊天记录交给 Claude，输出按话题聚类的摘要 + 黑话注释。

用法:
    python digest.py discord-20260726.enriched.txt
    python digest.py in.txt -o digest.md --model claude-sonnet-5

超长输入会自动分块：先分别摘要，再合并成一份。
"""

import argparse
import os
import sys
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5"
CHUNK_CHARS = 120_000   # 单块字符上限，中文约等于 token 数量级
MAX_TOKENS = 8000

SYSTEM = """你在帮一个刚加入交易讨论群的人整理群聊记录。他有量化金融背景，
数学和模型不用解释，但对这个圈子的中文交易黑话、美股板块叙事、群内成员的立场不熟悉。

聊天记录的格式：
  时间 发言人
    ↩ 回复 某人:「被引用的内容」
    正文
    [图片#N: 图片内容的文字转写]
    [EMBED] 转发链接的标题和摘要

注意：群聊里多个话题是交叉进行的，同一时间段的相邻消息可能属于完全不同的讨论。
你的核心工作就是把它们拆开还原成独立的线索。"""

PROMPT = """请把下面的群聊记录整理成一份日报，用中文，markdown 格式：

## 一句话概览
今天群里最主要在争论/关注什么，2-3 句。

## 话题线索
把讨论拆成若干独立线索，每条按重要性排序（参与人多、争论激烈、涉及具体仓位的优先）。
每条包含：
- **标题** —— 一句话点出争议焦点，不要写成"关于XX的讨论"这种空话
- **主要参与者**及各自立场（谁多谁空、谁的观点被反驳了）
- **核心论据**：正反双方分别拿出了什么理由，尽量保留具体数字、标的代码、时间点
- **结论**：达成共识 / 各执一词 / 没有结论

## 提到的具体标的与事件
列出被点名的股票代码、ETF、宏观事件及对应的观点。用表格。

## 黑话与术语注释
挑出记录里出现的行话、缩写、圈内梗、人名指代，逐条解释。
包括中文交易俚语（比如"做T""杀估值""马后炮"）和被简称的人物/机构。
这一节是给学习者看的，宁可多写。

## 值得追问的地方
有哪些说法是断言但没给论据的，或者哪些结论依赖了未经验证的前提。

要求：
- 严格基于记录内容，不要补充记录里没有的市场信息或你自己的判断
- 如果某个说法只是某人的个人观点而非群内共识，明确标注是谁说的
- 引用原话时用引号并注明发言人

---

{content}"""

MERGE_PROMPT = """下面是同一天群聊记录分段生成的多份摘要。请合并成一份完整日报，
保持原来的章节结构。合并时：把跨段落延续的同一个话题线索合并成一条，
去掉重复的术语注释，重新按重要性排序话题。

---

{content}"""


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


def call(client, model: str, prompt: str) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("需要设置 ANTHROPIC_API_KEY")

    from anthropic import Anthropic
    client = Anthropic()

    text = args.input.read_text(encoding="utf-8")
    parts = chunk(text, CHUNK_CHARS)

    if len(parts) == 1:
        result = call(client, args.model, PROMPT.format(content=parts[0]))
    else:
        print(f"输入较长，分 {len(parts)} 段处理")
        partials = []
        for i, p in enumerate(parts, 1):
            print(f"  处理第 {i}/{len(parts)} 段…")
            partials.append(call(client, args.model, PROMPT.format(content=p)))
        print("  合并…")
        joined = "\n\n---\n\n".join(partials)
        result = call(client, args.model, MERGE_PROMPT.format(content=joined))

    out = args.output or args.input.with_suffix(".digest.md")
    out.write_text(result, encoding="utf-8")
    print(f"完成 -> {out}")


if __name__ == "__main__":
    main()
