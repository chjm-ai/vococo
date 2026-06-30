"""System prompt 组装。

用 SDK 的 preset 形式:保留 Claude Code 原生 system prompt(里面含「如何使用
skill / 工具」的指令,这样你 ~/.claude 的 skill 才会被主动调用),再 append 上
Hermes 人格 + AI_BRAIN/USER.md 画像。
"""
from __future__ import annotations

from .. import config

PERSONA = """

=== 你的身份(claude-hermes)===
你是 Wesley 的个人 AI 助理(代号 Hermes),不是通用编码工具。
- 一律用中文回答,简洁直接,优先用表格/列表/代码块。
- 给可执行方案,不给模糊建议。
- 你是 Wesley 私人自用的助手,可直接、坦诚、有主见。
- 需要时主动调用合适的 skill 帮他把事办了。"""


def _load_user_profile() -> str:
    """读 AI_BRAIN/USER.md 作为长期画像。缺失则跳过。"""
    user_md = config.AI_BRAIN_DIR / "USER.md"
    try:
        text = user_md.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    return f"\n\n=== 关于 Wesley(来自 AI_BRAIN/USER.md)===\n{text}" if text else ""


def build_system_prompt() -> dict:
    """返回 SDK 的 preset system prompt(claude_code 默认 + append 人格/画像)。"""
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": PERSONA + _load_user_profile(),
    }
