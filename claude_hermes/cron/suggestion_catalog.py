"""内置起步自动化目录 —— 首次启动时作为建议(catalog 来源)播种。

每条都是"待用户一键接受"的定时任务模板;用户不接受就永远只是个建议,
绝不自动开跑。dedup_key 稳定,忽略后不再提。
"""
from __future__ import annotations

from . import suggestions

# (title, description, cron 表达式, 任务 prompt)
_CATALOG: list[tuple[str, str, str, str]] = [
    (
        "晨间简报",
        "每天早 8 点,把今天值得关注的事(日历/待办/邮件要点)汇总推给你。",
        "0 8 * * *",
        "现在是晨间简报时间。查一下今天的日历、待办和重要邮件,用简短分点汇总"
        "今天需要我关注/提醒的事。没什么可报的就说一句'今天没特别的'。",
    ),
    (
        "每周复盘",
        "每周日晚 9 点,回顾这一周聊过/做过的事,提炼进展与下周重点。",
        "0 21 * * 0",
        "现在是每周复盘。回顾这一周我们的对话和你帮我做的事,提炼:本周进展、"
        "踩的坑、下周该推进的重点。简短分点。",
    ),
]


def seed() -> int:
    """把目录条目登记成 catalog 建议(已提过的会被 dedup 跳过)。返回新增条数。"""
    added = 0
    for title, desc, cron, prompt in _CATALOG:
        rec = suggestions.add_suggestion(
            title=title,
            description=desc,
            source="catalog",
            job_spec={
                "name": title,
                "prompt": prompt,
                "schedule": {"kind": "cron", "expr": cron},
            },
            dedup_key=f"catalog:{title}",
        )
        if rec is not None:
            added += 1
    return added
