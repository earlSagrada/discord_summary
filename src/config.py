"""中央配置：统一路径 + 加载 .env（无第三方依赖）。

所有脚本顶部 `import config` 即可：自动把项目根的 .env 里的 KEY=VALUE
读进 os.environ（不覆盖已存在的环境变量），并暴露统一的目录常量。
"""

import os
import sys
from pathlib import Path

# Windows 控制台默认 cp1252，输出中文/emoji 会崩 → 统一切 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
INBOX_DIR = DATA_DIR / "inbox"
CACHE_DIR = DATA_DIR / "cache"
CHATS_DIR = DATA_DIR / "chats_by_date"
CHAT_FRANK_DIR = DATA_DIR / "chat_frank"
ENV_FILE = PROJECT_ROOT / ".env"
# 用户维护的宏观事件日期表（FOMC 等；每年更新一次）。committed，非运行时缓存。
EVENT_CALENDAR = PROJECT_ROOT / "event_calendar.json"


def load_env(path: Path | None = None) -> None:
    """把 .env 的 KEY=VALUE 读进环境变量（已存在的不覆盖，方便临时用 $env: 覆写）。"""
    path = path or ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


load_env()


# ───────────────────────── 信号新鲜度 / priced-in 阈值 ─────────────────────────
# 现价越过入场位超过这个比例 → 视为"已延伸/追高"（会按杠杆倍数放大，见 signals.py）
EXTENSION_PCT_MAX = 3.0
# 突破发生在这么多个交易日之前 → 视为"不新鲜"（旧突破，多半已被 price in）
BREAKOUT_STALE_DAYS = 2
# 聊天里最后一次提到该标的距今超过这么多分钟 → 标"聊天已不热"
STALE_CHAT_MINUTES = 180
# 财报 beat/miss 距今超过这么多天 → 催化视为已消化
EARNINGS_STALE_DAYS = 3

# ───────────────────────── 期权确认（B 档，只减分/提示，不加分）─────────────────────────
# yfinance 免费期权链。IV 是小数：0.60 = 60%。3x ETP 的 IV 天然更高，只做风险提示。
OPTIONS_ENABLED = True
IV_HIGH = 0.60
# 突破入场位上方这么近就有 call wall → 上方有盖子，绿灯保守降黄灯
CALL_WALL_CAP_PCT = 1.5

# ───────────────────────── 回测（outcomes 回填）─────────────────────────
# 回填的持有周期（交易日）
BACKTEST_HORIZONS = (1, 3, 5)
# 默认止盈目标 = 风险的多少倍（用于信号卡的 reward target 展示）
REWARD_R_MULTIPLE = 2.0

# ───────────────────────── 分级磁盘清理 ─────────────────────────
# 原始导出已合并进 chats_by_date，短期留存便于排查。
KEEP_INBOX_PROCESSED_DAYS = 15
# 行情/期权/事件缓存按天命名，只有当天文件会被读取。
KEEP_MARKET_CACHE_DAYS = 3
# 图片原图已转写进 transcripts.json，保留一周供人工复核。
KEEP_CACHE_IMAGES_DAYS = 7
# 运行日志超过阈值时保留尾部行，完整旧日志放到 .1。
LOG_MAX_MB = 5
LOG_KEEP_LINES = 2000


# ───────────────────────── 可调参数注册表（自我优化用）─────────────────────────
# src/optimize.py 允许 AI 调整这些阈值，但**只能写 data/tunables.json**，
# 永远不碰 Python 源码。好处：
#   1. 有上下界校验，AI 提出离谱数值会被直接拒绝；
#   2. 删掉 tunables.json 就整体回到出厂默认，天然可回滚；
#   3. 改了什么一目了然（diff 一个小 JSON，而不是 diff 源码）。
# 想让某个参数不可被 AI 调整，把它从这里移除即可。

TUNABLES: dict[str, dict] = {
    "EXTENSION_PCT_MAX": {
        "min": 1.0, "max": 10.0, "cast": float,
        "desc": "现价高出入场位多少百分比算追高（会按杠杆倍数放大）"},
    "BREAKOUT_STALE_DAYS": {
        "min": 1, "max": 10, "cast": int,
        "desc": "突破发生在几个交易日前就算不新鲜"},
    "STALE_CHAT_MINUTES": {
        "min": 30, "max": 1440, "cast": int,
        "desc": "标的最后一次被提及超过多少分钟算聊天已冷"},
    "EARNINGS_STALE_DAYS": {
        "min": 1, "max": 30, "cast": int,
        "desc": "财报催化超过多少天算已消化"},
    "IV_HIGH": {
        "min": 0.2, "max": 2.0, "cast": float,
        "desc": "隐含波动率高于多少算贵（小数，0.6=60%）"},
    "CALL_WALL_CAP_PCT": {
        "min": 0.3, "max": 10.0, "cast": float,
        "desc": "入场位上方多近有 call wall 就算有盖子"},
    "REWARD_R_MULTIPLE": {
        "min": 1.0, "max": 5.0, "cast": float,
        "desc": "止盈目标是风险的多少倍"},
}

TUNABLES_FILE = DATA_DIR / "tunables.json"
# 记录哪些参数当前被覆盖了（供 optimize.py 展示和回滚）
ACTIVE_OVERRIDES: dict[str, object] = {}


def tunable_defaults() -> dict[str, object]:
    """出厂默认值（本文件里写死的那些）。"""
    return {k: _TUNABLE_DEFAULTS[k] for k in TUNABLES}


def clamp_tunable(name: str, value) -> object:
    """按注册表把值转型并夹到合法区间；名字不在注册表里就抛错。"""
    spec = TUNABLES.get(name)
    if spec is None:
        raise KeyError(f"{name} 不是可调参数")
    val = spec["cast"](value)
    return max(spec["min"], min(spec["max"], val))


def _load_tunables() -> None:
    """把 data/tunables.json 的覆盖值应用到本模块的全局变量上。

    校验失败的条目**跳过并告警**，绝不让一个坏值把整条管线带崩。
    """
    if not TUNABLES_FILE.exists():
        return
    import json
    try:
        raw = json.loads(TUNABLES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[config] tunables.json 读取失败，忽略：{e}", file=sys.stderr)
        return
    for name, value in (raw.get("values") or {}).items():
        try:
            clamped = clamp_tunable(name, value)
        except (KeyError, TypeError, ValueError) as e:
            print(f"[config] 忽略无效可调参数 {name}={value!r}：{e}", file=sys.stderr)
            continue
        globals()[name] = clamped
        ACTIVE_OVERRIDES[name] = clamped


_TUNABLE_DEFAULTS = {k: globals()[k] for k in TUNABLES}
_load_tunables()
