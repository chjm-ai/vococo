"""配置加载:读 .env,锁定订阅认证,暴露模型/路径常量。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录的 .env(覆盖系统已有同名变量)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=True)


class ConfigError(RuntimeError):
    """配置缺失或冲突。"""


def _ensure_subscription_auth() -> str:
    """确保走订阅而非 API 按量计费。

    - 必须有 CLAUDE_CODE_OAUTH_TOKEN
    - 主动移除 ANTHROPIC_API_KEY(只要它在,SDK 就走 API 计费)
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "缺少 CLAUDE_CODE_OAUTH_TOKEN。\n"
            "  解决:本机跑 `claude setup-token`(需 Claude Pro/Max 订阅),\n"
            "  把生成的 sk-ant-oat01-... 写进项目根目录的 .env。"
        )
    if os.environ.pop("ANTHROPIC_API_KEY", None):
        # 移除以保证走订阅,而非按量计费
        pass
    return token


def _parse_chat_ids(raw: str) -> set[int]:
    """'123,-456 789' -> {123, -456, 789}。空 = 不限制(危险,会警告)。"""
    out: set[int] = set()
    for tok in raw.replace(",", " ").split():
        tok = tok.strip()
        if tok.lstrip("-").isdigit():
            out.add(int(tok))
    return out


def _parse_bool(raw: str, default: bool) -> bool:
    s = raw.strip().lower()
    if not s:
        return default
    return s not in ("0", "false", "no", "off")


def _parse_skills(raw: str) -> list[str] | str | None:
    """AGENT_SKILLS:空=None(全量,CLI 默认 ~全部 skill);'all'=显式全部;
    其余按逗号/空白拆成白名单(只挂这些,瘦身 tool schema)。"""
    s = raw.strip()
    if not s:
        return None
    if s.lower() == "all":
        return "all"
    return [t.strip() for t in s.replace(",", " ").split() if t.strip()]


OAUTH_TOKEN: str = _ensure_subscription_auth()
MODEL: str = os.environ.get("AGENT_MODEL", "claude-opus-4-8").strip()
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "40"))
# 工具权限模式:bypassPermissions=自动执行工具(shell/读写等),个人本机助理用
# 想更保守可设 acceptEdits(只自动接受文件编辑)。
PERMISSION_MODE: str = os.environ.get("AGENT_PERMISSION_MODE", "bypassPermissions").strip()
AI_BRAIN_DIR: Path = Path(
    os.path.expanduser(os.environ.get("AI_BRAIN_DIR", "~/AI_BRAIN"))
)

# 运行时数据目录(prompt_toolkit 历史等)
DATA_DIR: Path = _ROOT / "data"

# === 调度 / 心跳 ===
CRON_JOBS_PATH: Path = DATA_DIR / "cron_jobs.json"
SUGGESTIONS_PATH: Path = DATA_DIR / "suggestions.json"  # 待用户接受的自动化建议
HEARTBEAT_PATH: Path = DATA_DIR / "heartbeat"
SCHEDULER_TICK_SEC: int = int(os.environ.get("SCHEDULER_TICK_SEC", "30"))

# === Telegram ===
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_CHAT_IDS: set[int] = _parse_chat_ids(
    os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
)

# === 会话统一(跨入口连续)===
# 开启时:CLI / TUI / Telegram / 飞书 都归到同一会话 SESSION_KEY,
# "飞书问一半切 CLI 接着聊" 成立(单用户自用的默认)。关闭则按 平台:chat 隔离。
UNIFY_SESSIONS: bool = _parse_bool(os.environ.get("UNIFY_SESSIONS", ""), True)
SESSION_KEY: str = os.environ.get("SESSION_KEY", "main").strip() or "main"


def resolve_session_key(platform: str, chat_id: object) -> str:
    """各入口统一经此取会话键。

    群聊(TG 群 chat_id 为负)永远独立成一个会话 = 一个群一个项目;
    私聊则按 UNIFY_SESSIONS:开统一则共享主会话,否则按平台隔离。
    """
    if platform == "telegram" and isinstance(chat_id, int) and chat_id < 0:
        return f"tg:{chat_id}"
    return SESSION_KEY if UNIFY_SESSIONS else f"{platform}:{chat_id}"


# === Skill 加载范围 ===
# 全量挂载会把 ~110 个 skill 都塞进 tool schema(费 token、可能超限)。
# 在 .env 配 AGENT_SKILLS=monthly-planner,things-assistant,... 只挂常用的。
SKILLS: list[str] | str | None = _parse_skills(os.environ.get("AGENT_SKILLS", ""))

# === 记忆晋升:定时反思(把会话沉淀成 AI_BRAIN 长期记忆)===
# 默认关:开启后按 REFLECT_CRON 定时回顾统一会话,让 agent 用 save_memory 沉淀。
REFLECT_ENABLED: bool = _parse_bool(os.environ.get("REFLECT_ENABLED", ""), False)
REFLECT_CRON: str = os.environ.get("REFLECT_CRON", "0 23 * * *").strip()
# 反思结果推送目标 "platform:chat_id"(可选;不配则只写日志)
REFLECT_TARGET: str = os.environ.get("REFLECT_TARGET", "").strip()
