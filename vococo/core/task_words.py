"""任务状态 → 中文文案的唯一词表。

以前 task_tools.py / notify.py / tasks.py 各自维护一份状态词表,新增一个状态
(比如 paused)得改三处,漏改不报错——tasks.py 那份还是普通 dict 取值,漏改直接
KeyError(2026-07-23 架构复盘)。收口成这一个模块,新状态只改这里。
"""
from __future__ import annotations

import re

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


# 观测用,不做自动改写:"等你/等您"紧跟一个本系统里只该由后台任务自己动手的动词
# (改代码/写文件/查资料/跑命令这类),几乎总是把"AI 在办、用户在等"说反成"AI 在等
# 用户"。不做静态自动改写——"等你确认/等你测一下"这类在有些场景下是合理的(任务
# 确实需要用户紧接着做点什么),规则分不清语境,硬改文本比留着不动风险更大;只留
# 一层日志,命中了打印出来,方便下次真机复现时直接从日志定位原文,而不是只能靠
# 事后转述(2026-07-31 真机复现过一次"等你改完",当时没有任何日志能佐证是哪句)。
_REVERSED_DIRECTION_RE = re.compile(
    r"等(你|您)(把|先)?.{0,6}?(改|写|弄|搞|跑|传|发|整理|汇总|生成)(完|好)?"
)


def flag_if_reversed_direction(text: str | None, where: str) -> None:
    """命中疑似"等你+动词"方向说反的话术就打一条日志,不改文本、不抛错。

    where:调用位置标识(比如 "notify._announce_text"),方便日志里区分是任务完成
    播报、平台推送文案,还是查询转述哪个环节生成的。"""
    if text and _REVERSED_DIRECTION_RE.search(text):
        print(f"[direction-guard] ⚠️ {where} 疑似方向说反(等你+动词):{text!r}", flush=True)
