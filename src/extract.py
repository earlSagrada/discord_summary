"""从 merged.enriched.txt 抽 ticker（allowlist + 黑话词典）。

只认 tickers.UNIVERSE 里的标的（含别名），避免把 FOMC/IV/AI 之类误判成代码。
另外把不认识的 `$CASHTAG` 收集起来，方便日后往 UNIVERSE 里加。
"""

import re
from collections import defaultdict

import config  # noqa: F401  (UTF-8 stdout + load .env)
import tickers as T

_CASHTAG = re.compile(r"\$([A-Za-z]{1,6})\b")


def _word_regex(keys: list[str]) -> re.Pattern | None:
    if not keys:
        return None
    alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z]){alt}(?![A-Za-z])", re.IGNORECASE)


def extract_mentions(text: str) -> tuple[list[dict], dict[str, int]]:
    latin = T.latin_alias_index()
    cjk = T.cjk_alias_index()
    word_re = _word_regex([k for k in latin if len(k) >= 3])  # 2 字母只经 cashtag/中文

    counts: dict[str, int] = defaultdict(int)
    samples: dict[str, list[str]] = defaultdict(list)
    unknown: dict[str, int] = defaultdict(int)

    for line in text.splitlines():
        hits: set[str] = set()
        for m in _CASHTAG.finditer(line):
            key = m.group(1).lower()
            if key in latin:
                hits.add(latin[key])
            else:
                unknown["$" + m.group(1).upper()] += 1
        if word_re:
            for m in word_re.finditer(line):
                hits.add(latin[m.group(0).lower()])
        for alias, tk in cjk.items():
            if alias in line:
                hits.add(tk)

        stripped = line.strip()
        for tk in hits:
            counts[tk] += 1
            if stripped and len(samples[tk]) < 3 and stripped not in samples[tk]:
                samples[tk].append(stripped[:160])

    mentions = [
        {
            "ticker": tk,
            "type": T.UNIVERSE[tk]["type"],
            "name": T.UNIVERSE[tk]["name"],
            "focus": T.is_focus(tk),
            "count": n,
            "samples": samples[tk],
        }
        for tk, n in counts.items()
    ]
    # 重点标的优先，再按提及次数
    mentions.sort(key=lambda m: (not m["focus"], -m["count"], m["ticker"]))
    return mentions, dict(unknown)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    txt = Path(sys.argv[1]).read_text(encoding="utf-8")
    ms, unk = extract_mentions(txt)
    for m in ms:
        tag = "★" if m["focus"] else " "
        print(f"{tag} {m['ticker']:8} {m['type']:13} ×{m['count']:<3} {m['name']}")
    if unk:
        print("\n未收录 cashtag：", unk)
