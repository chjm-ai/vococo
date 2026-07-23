"""任务状态 → 中文文案的唯一词表。

以前 task_tools.py / notify.py / tasks.py 各自维护一份状态词表,新增一个状态
(比如 paused)得改三处,漏改不报错——tasks.py 那份还是普通 dict 取值,漏改直接
KeyError(2026-07-23 架构复盘)。收口成这一个模块,新状态只改这里。
"""
from __future__ import annotations

STATUS_WORD: dict[str, str] = {
    "queued": "排队中",
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

# 平台推送(Telegram/Web 文字消息)专用的状态 emoji,只标终态;非终态没有推送场景。
STATUS_EMOJI: dict[str, str] = {
    "done": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}


def status_word(status: str) -> str:
    """状态码 → 中文词;未知状态原样返回,不抛错(状态码本身在文案里也读得通)。"""
    return STATUS_WORD.get(status, status)


def status_emoji(status: str) -> str:
    """状态码 → emoji;非终态/未知状态用通用图标兜底。"""
    return STATUS_EMOJI.get(status, "📋")
