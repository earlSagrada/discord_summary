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
