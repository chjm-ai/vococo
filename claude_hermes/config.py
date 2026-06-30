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

# === Telegram ===
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_CHAT_IDS: set[int] = _parse_chat_ids(
    os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
)
