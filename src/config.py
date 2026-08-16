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
