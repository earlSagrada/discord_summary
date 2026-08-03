"""标的宇宙 + 黑话/别名词典。

重点在 ETP/ETF（杠杆/板块 ETF），同时收录群里点名过的个股，便于回看原文。
匹配策略见 extract.py：
- latin 别名（含 ticker 本身）按**单词边界、大小写不敏感**匹配；
- cjk 别名按**子串**匹配；
- 2 字母 ticker（BE/MU 等）只经 `$cashtag` 或别名命中，避免把英文词误判成标的。

来源：trade_notes/交易信号复盘与方法总结.md（案例与 §6 黑话表）。
"""

# ticker -> {type: etp|etf|stock|index_future, name, aliases:[...]}
# type in {"etp","etf"} 视为「重点」（focus），signals.py 默认只跑这些。
# etp 可带 lev（杠杆倍数，默认 1）；利空不跌阈值按 lev 放大，避免杠杆 ETP 常规波动误触发。
UNIVERSE: dict[str, dict] = {
    # ── 杠杆 / 板块 ETP·ETF（重点） ──
    "SOXL": {"type": "etp", "lev": 3, "name": "3x 半导体多", "aliases": ["soxl"]},
    "SOXS": {"type": "etp", "lev": 3, "name": "3x 半导体空", "aliases": ["soxs", "soxs价投"]},
    "TQQQ": {"type": "etp", "lev": 3, "name": "3x 纳指多", "aliases": ["tqqq"]},
    "SQQQ": {"type": "etp", "lev": 3, "name": "3x 纳指空", "aliases": ["sqqq"]},
    "SPXL": {"type": "etp", "lev": 3, "name": "3x 标普多", "aliases": ["spxl"]},
    "SPXS": {"type": "etp", "lev": 3, "name": "3x 标普空", "aliases": ["spxs"]},
    "NVDL": {"type": "etp", "lev": 2, "name": "2x NVDA 多", "aliases": ["nvdl"]},
    "TSLL": {"type": "etp", "lev": 2, "name": "2x TSLA 多", "aliases": ["tsll"]},
    "FNGU": {"type": "etp", "lev": 3, "name": "3x FANG+ 多", "aliases": ["fngu"]},
    "LABU": {"type": "etp", "lev": 3, "name": "3x 生科多", "aliases": ["labu"]},
    "QQQ":  {"type": "etf", "name": "纳指100", "aliases": ["qqq"]},
    "SPY":  {"type": "etf", "name": "标普500", "aliases": ["spy"]},
    "IWM":  {"type": "etf", "name": "罗素2000", "aliases": ["iwm"]},
    "SMH":  {"type": "etf", "name": "半导体", "aliases": ["smh"]},
    "SOXX": {"type": "etf", "name": "半导体", "aliases": ["soxx"]},
    "XLK":  {"type": "etf", "name": "科技", "aliases": ["xlk"]},
    "BOXX": {"type": "etf", "name": "类现金避险（睡觉/空仓代名词）", "aliases": ["boxx"]},
    "SKUU": {"type": "etp", "name": "存疑·待 yfinance 校验", "aliases": ["skuu"]},
    "7709.HK": {"type": "etp", "lev": 2, "name": "南方2x海力士（港股杠杆）", "aliases": ["7709"]},

    # ── 个股（非重点，仅收录/回看） ──
    "NVDA": {"type": "stock", "name": "英伟达", "aliases": ["nvda", "轰达", "老黄", "黄氏", "英伟达"]},
    "GOOG": {"type": "stock", "name": "谷歌", "aliases": ["goog", "googl", "谷歌"]},
    "ORCL": {"type": "stock", "name": "甲骨文", "aliases": ["orcl", "甲骨文"]},
    "NBIS": {"type": "stock", "name": "Nebius", "aliases": ["nbis"]},
    "AXTI": {"type": "stock", "name": "AXT", "aliases": ["axti"]},
    "BE":   {"type": "stock", "name": "Bloom Energy", "aliases": []},   # 太常见，只经 $BE
    "GEV":  {"type": "stock", "name": "GE Vernova", "aliases": ["gev"]},
    "MU":   {"type": "stock", "name": "美光", "aliases": ["镁光", "美光"]},  # 'mu' 太常见，只经 $MU/中文
    "MXL":  {"type": "stock", "name": "MaxLinear", "aliases": ["mxl"]},
    "SNDK": {"type": "stock", "name": "SanDisk", "aliases": ["sndk", "闪迪"]},
    "AAPL": {"type": "stock", "name": "苹果", "aliases": ["aapl", "苹果"]},
    "MSTR": {"type": "stock", "name": "MicroStrategy", "aliases": ["mstr"]},
    "TSM":  {"type": "stock", "name": "台积电", "aliases": ["tsm", "tsmc", "台积电"]},
    "TCOM": {"type": "stock", "name": "携程", "aliases": ["tcom", "携程"]},

    # ── 指数期货（有 bar，无期权） ──
    "NQ=F": {"type": "index_future", "name": "纳指期货", "aliases": ["纳指期货"]},  # 'nq' 太常见
    "ES=F": {"type": "index_future", "name": "标普期货", "aliases": ["标普期货"]},
}

# 双保险：即使误进 bare-uppercase 分支也直接丢弃这些
STOPWORDS: set[str] = {
    "FOMC", "CPI", "PPI", "PCE", "GDP", "ISM", "PMI", "EPS", "IV", "ATH", "ATL",
    "KOL", "ETF", "ETP", "ADR", "LTA", "USD", "CNY", "HKD", "AI", "PT", "DTE",
    "VWAP", "GEX", "OI", "TP", "SL", "FUD", "FOMO", "YTD", "EOD", "AH", "PM",
    "US", "UK", "EU", "CEO", "CFO", "IPO", "API", "URL", "OK", "LOL", "IMO",
}

FOCUS_TYPES = {"etp", "etf"}


def latin_alias_index() -> dict[str, str]:
    """lowercase latin 别名/ticker -> 规范 ticker。仅收 ASCII 别名。"""
    idx: dict[str, str] = {}
    for tk, meta in UNIVERSE.items():
        base = tk.split(".")[0].split("=")[0]  # 7709.HK->7709, NQ=F->NQ（供 cashtag/别名，不进 bare 分支）
        for a in [base, *meta["aliases"]]:
            if a.isascii() and any(c.isalpha() for c in a):
                idx[a.lower()] = tk
    return idx


def cjk_alias_index() -> dict[str, str]:
    """CJK / 含数字 别名 -> 规范 ticker（子串匹配用）。"""
    idx: dict[str, str] = {}
    for tk, meta in UNIVERSE.items():
        for a in meta["aliases"]:
            if not a.isascii() or (a.isascii() and not a.isalpha()):
                idx[a] = tk
    return idx


def is_focus(ticker: str) -> bool:
    meta = UNIVERSE.get(ticker)
    return bool(meta and meta["type"] in FOCUS_TYPES)
