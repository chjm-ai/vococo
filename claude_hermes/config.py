"""配置加载:读 .env,锁定订阅认证,暴露模型/路径常量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录的 .env(覆盖系统已有同名变量)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=True)


class ConfigError(RuntimeError):
    """配置缺失或冲突。"""


def _ensure_subscription_auth(require: bool = True) -> str:
    """确保走订阅而非 API 按量计费。

    - require=True 时必须有 CLAUDE_CODE_OAUTH_TOKEN(只用 Claude 官方订阅的默认场景)
    - require=False(已配了第三方 provider 的 key)时 OAUTH 可缺,返回空串
    - 无论如何都移除 ANTHROPIC_API_KEY(只要它在,走 claude 模型时就会误按量计费;
      第三方 provider 的鉴权在每轮 options.env 里单独注入,不依赖它)
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token and require:
        raise ConfigError(
            "缺少 CLAUDE_CODE_OAUTH_TOKEN。\n"
            "  解决:本机跑 `claude setup-token`(需 Claude Pro/Max 订阅),\n"
            "  把生成的 sk-ant-oat01-... 写进项目根目录的 .env。\n"
            "  (若只用 DeepSeek/Kimi 等第三方端点,配好对应 API key 即可免此项。)"
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


def _oauth_required() -> bool:
    """订阅 token 是否必需。

    仅当 cc-switch 当前激活的是一个可用的第三方供应商(DeepSeek/Kimi 等,带 key)
    时可免——那种场景下这一轮走第三方端点,不需要 Claude 订阅。其余情况(激活官方、
    未装 cc-switch、读取异常)一律要求订阅 token,保持原有行为。
    """
    try:
        from . import providers

        return not providers.has_active_third_party()
    except Exception:  # noqa: BLE001 —— 读取出问题时保守要求订阅
        return True


OAUTH_TOKEN: str = _ensure_subscription_auth(require=_oauth_required())
MODEL: str = os.environ.get("AGENT_MODEL", "claude-opus-4-8").strip()
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "40"))
# 工具权限模式:bypassPermissions=自动执行工具(shell/读写等),个人本机助理用
# 想更保守可设 acceptEdits(只自动接受文件编辑)。
PERMISSION_MODE: str = os.environ.get("AGENT_PERMISSION_MODE", "bypassPermissions").strip()
# 单轮硬超时(秒):卡死也能恢复。clarify 等用户回复会占用本轮,故放宽到 10 分钟。
AGENT_TURN_TIMEOUT: int = int(os.environ.get("AGENT_TURN_TIMEOUT", "3600"))
# ask_user 等用户回复的超时(秒),须 < AGENT_TURN_TIMEOUT,好让 clarify 先返回、本轮别被硬砍。
CLARIFY_TIMEOUT: int = int(os.environ.get("CLARIFY_TIMEOUT", "300"))
# 危险命令拦截(PreToolUse hook):默认开,只拦灾难级命令(删根/格式化/强推等)。
DANGER_GUARD: bool = _parse_bool(os.environ.get("DANGER_GUARD", ""), True)
# 审批闸(PreToolUse hook):默认开。对「危险但非灾难」的操作(写工作目录外、
# git push/reset --hard、rm -rf、包安装、curl|sh)在【有交互通道时】弹按钮请你批准;
# 无交互通道(CLI/eval/cron)则放行(信任该通道)。关掉 = 回到纯 bypass。
APPROVAL_GATE: bool = _parse_bool(os.environ.get("APPROVAL_GATE", ""), True)
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

# === Web 渠道(手机浏览器访问,自建 UI)===
# 一个进程内起 aiohttp:SSE 流式 + 会话侧边栏。默认只监听 127.0.0.1,
# 靠 Cloudflare Tunnel / Tailscale 把本地端口安全暴露到公网(带 HTTPS)。
WEB_ENABLED: bool = _parse_bool(os.environ.get("WEB_ENABLED", ""), False)
WEB_HOST: str = os.environ.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_PORT: int = int(os.environ.get("WEB_PORT", "8848"))
# 访问口令:非空则每次数据请求都要带上(浏览器首次输入后记住)。留空=不校验(仅本机调试用)。
WEB_AUTH_TOKEN: str = os.environ.get("WEB_AUTH_TOKEN", "").strip()

# === Web Push 系统通知(iOS 16.4+ 已装到主屏的 PWA / Android / 桌面)===
# 页面关了、锁屏了也能弹系统通知(SSE 只在页面开着时能推)。
# 生成密钥:python -m claude_hermes.gateway.adapters.web_push --gen-keys
# 把打印出的两串填进 .env;留空则通知功能自动关闭,不影响其余功能。
VAPID_PUBLIC_KEY: str = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY: str = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT: str = (
    os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()
    or "mailto:admin@example.com"
)
# 四种通知场景各自开关(默认全开;某类嫌吵就在 .env 设 0 关掉)
PUSH_ON_DONE: bool = _parse_bool(os.environ.get("PUSH_ON_DONE", ""), True)  # 回复完成
PUSH_ON_APPROVAL: bool = _parse_bool(os.environ.get("PUSH_ON_APPROVAL", ""), True)  # 需审批
PUSH_ON_PROACTIVE: bool = _parse_bool(os.environ.get("PUSH_ON_PROACTIVE", ""), True)  # 主动/cron
PUSH_ON_ERROR: bool = _parse_bool(os.environ.get("PUSH_ON_ERROR", ""), True)  # 出错

# === 语音输入(手机录音 → 转文字)===
# Claude 不吃音频,故录音上传后端先转成文字再进对话。
# 默认走 SiliconFlow 的 SenseVoice(OpenAI 兼容接口、中文准、国内直连、近免费)。
# 去 siliconflow.cn 注册拿免费 key,填到 .env 的 SILICONFLOW_API_KEY。留空则语音按钮报错提示未配置。
STT_API_KEY: str = os.environ.get("SILICONFLOW_API_KEY", "").strip()
STT_BASE_URL: str = (
    os.environ.get("STT_BASE_URL", "https://api.siliconflow.cn/v1").strip()
    or "https://api.siliconflow.cn/v1"
)
STT_MODEL: str = (
    os.environ.get("STT_MODEL", "FunAudioLLM/SenseVoiceSmall").strip()
    or "FunAudioLLM/SenseVoiceSmall"
)

# === 会话统一(跨入口连续)===
# 开启时:CLI / TUI / Telegram / 飞书 都归到同一会话 SESSION_KEY,
# "飞书问一半切 CLI 接着聊" 成立(单用户自用的默认)。关闭则按 平台:chat 隔离。
UNIFY_SESSIONS: bool = _parse_bool(os.environ.get("UNIFY_SESSIONS", ""), True)
SESSION_KEY: str = os.environ.get("SESSION_KEY", "main").strip() or "main"


def resolve_session_key(platform: str, chat_id: object) -> str:
    """各入口统一经此取会话键。

    群聊(TG 群 chat_id 为负)永远独立成一个会话 = 一个群一个项目;
    Web 端自带多会话管理:每个对话独立成 web:<conv_id>,不受 UNIFY 影响;
    但特殊 conv_id "main" 汇入统一主会话(从网页也能接着 TG/CLI 那条线聊);
    私聊则按 UNIFY_SESSIONS:开统一则共享主会话,否则按平台隔离。
    """
    if platform == "telegram" and isinstance(chat_id, int) and chat_id < 0:
        return f"tg:{chat_id}"
    if platform == "web":
        return SESSION_KEY if chat_id == "main" else f"web:{chat_id}"
    return SESSION_KEY if UNIFY_SESSIONS else f"{platform}:{chat_id}"


def project_root_for(session_key: str) -> str | None:
    """项目会话绑定的仓库根目录(不含 worktree);非项目会话返回 None。

    项目会话 key 形如 web:p<hash>:<conv>(三段);其余(main/默认项目的老
    web:<conv>/TG/CLI)都不带项目哈希,返回 None。反查走 session_store 的
    哈希→路径 映射表(延迟 import 打破 config↔store 循环依赖)。
    """
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "web" and len(parts[1]) > 1 and parts[1][0] == "p":
        from .memory import session_store

        return session_store.path_for_hash(parts[1][1:])
    return None


def project_cwd_for(session_key: str) -> str | None:
    """会话实际工作目录(cwd):优先该会话独占的 worktree,否则回退项目根。

    每会话一 worktree(见 core.worktree)后,同一项目下不同会话各在各的物理目录、
    各在各的分支 —— cwd 从「按项目」升级为「按会话」,这是隔离的关键一步。
    worktree 还没建(会话没发过消息)或已被清理 → 回退项目根,行为同旧版。
    """
    root = project_root_for(session_key)
    if root is None:
        return None
    from .memory import session_store

    wt = session_store.get_worktree(session_key)
    return wt if wt and os.path.isdir(wt) else root


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
