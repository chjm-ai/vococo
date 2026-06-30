"""System prompt 组装:人格 + AI_BRAIN 画像。

M0 只注入 USER.md 画像;会话记忆 / AI_BRAIN memory 检索留到 M1。
"""
from __future__ import annotations

from .. import config

PERSONA = """你是 Wesley 的个人 AI 助理(代号 Hermes)。
- 一律用中文回答,简洁直接,优先用表格/列表/代码块。
- 给可执行方案,不给模糊建议。
- 你是 Wesley 自己用的私人助手,可以直接、坦诚、有主见。"""


def _load_user_profile() -> str:
    """读 AI_BRAIN/USER.md 作为长期画像。缺失则跳过。"""
    user_md = config.AI_BRAIN_DIR / "USER.md"
    try:
        text = user_md.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    if not text:
        return ""
    return f"\n\n=== 关于 Wesley(来自 AI_BRAIN/USER.md)===\n{text}"


def build_system_prompt() -> str:
    """拼出本轮 system prompt。"""
    return PERSONA + _load_user_profile()
