"""喂给 AI 的 prompt 统一放在项目根的 prompts/ 目录，一个用途一个文件。

用法:
    import prompts
    system = prompts.load("digest_system.md")
    user   = prompts.load("digest_user.md").format(content=text)

好处：改 prompt 不用动代码；不同脚本共享同一份措辞；便于版本管理。
"""

from functools import lru_cache

import config

PROMPTS_DIR = config.PROJECT_ROOT / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """读取 prompts/<name> 的全文（带缓存）。找不到时报清晰的错误。"""
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"缺少 prompt 文件：{path}")
    return path.read_text(encoding="utf-8")
