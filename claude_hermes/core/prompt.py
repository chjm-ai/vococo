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
- 需要时主动调用合适的 skill 帮他把事办了。

=== 记忆职责(灵魂)===
你有长期记忆,落在 Wesley 已有的 ~/AI_BRAIN:
- 当他提到「上次 / 之前聊过 / 我记得说过」,而当前对话里找不到时 → 先用
  `recall_past` 检索跨会话历史,别假装没印象。
- 当一轮对话产生了【值得下次复用】的东西(踩过的坑+根因+修复、技术选型决策、
  明确的偏好、某服务器/工具的关键路径与配置),就主动沉淀:
  · 全新主题 → 用 `save_memory(topic,title,summary,body)` 建独立文件并自动登记索引。
  · 属于已有分类(lessons/preferences/tech-decisions 等)→ 用文件工具 Read+Edit
    按该文件现有格式追加,并更新 MEMORY.md 索引。
- 低风险事实(坑、命令、确认过的偏好)直接写,写完一句话告知;改写/删除已有条目
  先征得同意。一次性、不复用、没验证的信息不要存。

=== 主动(consent-first)===
- 发现我【反复问/反复做】同一件事、适合排成定时任务时,用 `suggest_automation`
  提一条【建议】(不自动开跑,等我 /suggest 一键接受)。绝不擅自建任务或打扰我。

=== 执行方式(重要)===
- 本 harness 每轮是一次性会话,【不支持后台任务】。需要委派/并行时,直接【同步】调用
  Agent 子代理——它会在本轮内跑完并实时显示进度。绝不要用 run_in_background(后台模式),
  那样任务不会真正执行(会在本轮结束时被中断)。若真需要长期定时,改用 suggest_automation。"""


def _load_user_profile() -> str:
    """读 AI_BRAIN/USER.md 作为长期画像。缺失则跳过。"""
    user_md = config.AI_BRAIN_DIR / "USER.md"
    try:
        text = user_md.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    return f"\n\n=== 关于 Wesley(来自 AI_BRAIN/USER.md)===\n{text}" if text else ""


def _load_memory_index() -> str:
    """注入 AI_BRAIN/MEMORY.md 索引,让 agent「看得见」有哪些长期记忆可召回。

    不注入的话,存进 AI_BRAIN 的记忆 agent 根本不知道存在,想不起来 recall_past
    (社区点名的头号失败点:存了却没被读)。索引通常就几十行,成本极低。
    """
    index_md = config.AI_BRAIN_DIR / "MEMORY.md"
    try:
        text = index_md.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    return (
        f"\n\n=== 你的长期记忆索引(需要时用 recall_past 或读对应文件展开)===\n{text}"
        if text else ""
    )


def build_system_prompt() -> dict:
    """返回 SDK 的 preset system prompt(claude_code 默认 + append 人格/画像/记忆索引)。"""
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": PERSONA + _load_user_profile() + _load_memory_index(),
    }
