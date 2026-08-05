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


# === 部署模式(2026-08 服务器版,见 docs/design/server-edition-tech-plan.md)===
# personal=主人本机单用户(默认,行为与改造前完全一致);server=多租户对外服务。
# 铁律:模式判断只允许出现在本文件、tenancy/ 包、各 store 的「后端选择」一处;
# 业务代码一律读 tenancy 抽象,不得各自 if MODE —— 分支散落 = 变相双份维护。
MODE: str = os.environ.get("VOCOCO_MODE", "personal").strip() or "personal"
IS_SERVER: bool = MODE == "server"


def _oauth_required() -> bool:
    """订阅 token 是否必需。

    仅当设置页里配置了可用的第三方供应商(DeepSeek/Kimi 等,带 key)时可免——那种
    场景下走第三方端点,不需要 Claude 订阅。其余情况(激活官方、未配置第三方、读取
    异常)一律要求订阅 token,保持原有行为。
    """
    try:
        from . import providers

        return not providers.has_active_third_party()
    except Exception:  # noqa: BLE001 —— 读取出问题时保守要求订阅
        return True


OAUTH_TOKEN: str = _ensure_subscription_auth(require=_oauth_required())
# 助理主人的称呼:注入 PERSONA / 工具描述,让助理知道在为谁服务。
# 开源默认「主人」,在 .env 设 VOCOCO_USER_NAME=你的名字 即个性化。
USER_NAME: str = os.environ.get("VOCOCO_USER_NAME", "主人").strip() or "主人"
# 助理人格代号:出现在 UI 标题/拦截提示/系统 prompt(见 core/prompt.py)。
# 默认「Wazir」,在 .env 设 VOCOCO_PERSONA_NAME 即可换成自己的人格名。
PERSONA_NAME: str = os.environ.get("VOCOCO_PERSONA_NAME", "Wazir").strip() or "Wazir"
MODEL: str = os.environ.get("AGENT_MODEL", "claude-sonnet-5").strip()
# 单轮 agentic 轮数上限,0=不限(交给 AGENT_TURN_TIMEOUT 硬超时兜底)。2026-07-10 起
# 默认放开:100 也照样截断过正经长任务,轮数不是好的成本闸,超时才是。
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "0"))
# 工具权限模式:bypassPermissions=自动执行工具(shell/读写等),个人本机助理用
# 想更保守可设 acceptEdits(只自动接受文件编辑)。
PERMISSION_MODE: str = os.environ.get("AGENT_PERMISSION_MODE", "bypassPermissions").strip()
# 单轮硬超时(秒):卡死也能恢复。clarify 等用户回复会占用本轮,故放宽到 10 分钟。
AGENT_TURN_TIMEOUT: int = int(os.environ.get("AGENT_TURN_TIMEOUT", "3600"))
# ask_user 等用户回复的超时(秒),须 < AGENT_TURN_TIMEOUT,好让 clarify 先返回、本轮别被硬砍。
CLARIFY_TIMEOUT: int = int(os.environ.get("CLARIFY_TIMEOUT", "300"))
# === 保温池(会话级常驻 ClaudeSDKClient,见 core/client_pool.py)===
# 空闲超过该秒数回收 CLI 子进程;300s 对齐 prompt cache 的 5 分钟 TTL。设 0 禁用保温池
# (回到每轮冷启动 + resume 的老路径,排障用)。
CLIENT_POOL_IDLE_TTL: int = int(os.environ.get("CLIENT_POOL_IDLE_TTL", "300"))
# 池容量上限:每个保温 client 是一个常驻 CLI 子进程(内存不小),超出踢最久未用的。
CLIENT_POOL_MAX: int = int(os.environ.get("CLIENT_POOL_MAX", "4"))
# 危险命令拦截(PreToolUse hook):默认开,只拦灾难级命令(删根/格式化/强推等)。
DANGER_GUARD: bool = _parse_bool(os.environ.get("DANGER_GUARD", ""), True)
# 审批闸(PreToolUse hook):默认开。对「危险但非灾难」的操作(写工作目录外、
# git push/reset --hard、rm -rf、包安装、curl|sh)在【有交互通道时】弹按钮请你批准;
# 无交互通道(CLI/eval/cron)则放行(信任该通道)。关掉 = 回到纯 bypass。
APPROVAL_GATE: bool = _parse_bool(os.environ.get("APPROVAL_GATE", ""), True)
AI_BRAIN_DIR: Path = Path(
    os.path.expanduser(os.environ.get("AI_BRAIN_DIR", "~/AI_BRAIN"))
)

# 本项目(vococo)仓库根目录——语音派后台任务时的默认 cwd(见 voice/task_tools.py):
# 没显式指定项目就落到这里,由 worktree 机制隔离,绝不在主检出目录上直接干活
ROOT_DIR: Path = _ROOT
# 运行时数据目录(prompt_toolkit 历史等)
DATA_DIR: Path = _ROOT / "data"
# 用户发的图片落盘目录(Web 端消息里的图片,持久化后刷新页面仍可见)
IMAGES_DIR: Path = DATA_DIR / "images"
# 用户发的音频落盘目录(转写后仍保留原文件供回放,见 memory/audio.py)
AUDIO_DIR: Path = DATA_DIR / "audio"
# 单个音频附件大小上限(转写走 AI 解读,不是原生多模态,过大只会拖垮转写接口)
AUDIO_MAX_BYTES: int = 100 * 1024 * 1024
# 会议录音转写(voice/stt.py):附件时长 ≥ 40 分钟按会议处理——paraformer-v2 异步
# 文件转写 + 说话人分离(diarization),个人短录音仍走 qwen3-asr-flash。paraformer
# 只接受公网可下载 URL,临时文件放 PUBLISHED_DIR 走 /pub 路由,随机名+转写完即删。
DASHSCOPE_MEETING_MODEL: str = (
    os.environ.get("DASHSCOPE_MEETING_MODEL", "paraformer-v2").strip() or "paraformer-v2"
)
MEETING_ASR_MIN_SECONDS: int = 40 * 60
# qwen3-asr-flash 的音频时长上限:实测 5 分钟 400("audio is too long")、4.5 分钟 200,
# 取 270s 留余量。超过走 paraformer(异步,见 transcribe_attachment 三分支)。
ASR_FLASH_MAX_SECONDS: float = 4.5 * 60
# 发布的静态网页(丢文件进来就能公网访问,见 gateway/adapters/web.py 的 /pub 路由)
PUBLISHED_DIR: Path = DATA_DIR / "published"

# vococo 专属内置 skill 插件(本地 SDK plugin,见 core/agent.py 的 ClaudeAgentOptions.plugins)。
# 跟 ~/.claude/skills 那套跨工具共享的个人 skill 库分开:装在这里的 skill 只有 vococo 自己的
# 会话能看到,Claude Code / Codex / OpenCode 读不到,适合"离了 vococo 这套服务就没意义"的
# skill(比如 vococo-web-publish,依赖本仓库的 data/published/ + /pub 路由)。
PLUGIN_DIR: Path = Path(__file__).resolve().parent / "plugin"

# === 调度 / 心跳 ===
CRON_JOBS_PATH: Path = DATA_DIR / "cron_jobs.json"
SUGGESTIONS_PATH: Path = DATA_DIR / "suggestions.json"  # 待用户接受的自动化建议
HEARTBEAT_PATH: Path = DATA_DIR / "heartbeat"
SCHEDULER_TICK_SEC: int = int(os.environ.get("SCHEDULER_TICK_SEC", "30"))

# === 假死看门狗(gateway/watchdog.py)===
# 事件循环无响应超过 DUMP 秒 → 把全线程堆栈写进 watchdog.log 留证据;
# 超过 EXIT 秒 → 自杀退出,由 run.sh 守护循环拉起。EXIT 留 3 分钟余量,
# 避免误杀偶发的长同步操作(如首次加载大模型权重)。
WATCHDOG_DUMP_SEC: int = int(os.environ.get("WATCHDOG_DUMP_SEC", "30"))
WATCHDOG_EXIT_SEC: int = int(os.environ.get("WATCHDOG_EXIT_SEC", "180"))
WATCHDOG_LOG_PATH: Path = DATA_DIR / "logs" / "watchdog.log"

# === Telegram ===
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_CHAT_IDS: set[int] = _parse_chat_ids(
    os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
)
# 白名单为空时的姿态:默认 fail-closed(拒收一切,防陌生人搜到 bot 就能驱动 Claude)。
# 真要「谁都能聊」必须显式设 TELEGRAM_ALLOW_ALL=1,让危险选项需要主动打开。
TELEGRAM_ALLOW_ALL: bool = _parse_bool(os.environ.get("TELEGRAM_ALLOW_ALL", ""), False)

# === Web 渠道(手机浏览器访问,自建 UI)===
# 一个进程内起 aiohttp:SSE 流式 + 会话侧边栏。默认只监听 127.0.0.1,
# 靠 Cloudflare Tunnel / Tailscale 把本地端口安全暴露到公网(带 HTTPS)。
WEB_ENABLED: bool = _parse_bool(os.environ.get("WEB_ENABLED", ""), False)
WEB_HOST: str = os.environ.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_PORT: int = int(os.environ.get("WEB_PORT", "8848"))
# 访问口令:非空则每次数据请求都要带上(浏览器首次输入后记住)。留空=不校验(仅本机调试用)。
WEB_AUTH_TOKEN: str = os.environ.get("WEB_AUTH_TOKEN", "").strip()
# 是否允许从 Web 设置页注册「本地 stdio MCP」(command+args 会被当子进程拉起 = 远程 RCE 的
# 第二条路)。默认 fail-closed 关闭;真要用就显式设 WEB_ALLOW_STDIO_MCP=1。sse/http 型不受限。
WEB_ALLOW_STDIO_MCP: bool = _parse_bool(os.environ.get("WEB_ALLOW_STDIO_MCP", ""), False)

# === Web Push 系统通知(iOS 16.4+ 已装到主屏的 PWA / Android / 桌面)===
# 页面关了、锁屏了也能弹系统通知(SSE 只在页面开着时能推)。
# 生成密钥:python -m vococo.gateway.adapters.web_push --gen-keys
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
# 识别本体:阿里 DashScope 的 qwen3-asr-flash(2026-07-08 从 SiliconFlow 的
# SenseVoiceSmall 切过来——真机实测识别环节要 8~17 秒,隔离测试证实是 SenseVoice
# 接口本身慢、不是我们代码的问题;qwen3-asr-flash 同等准确度下只要 0.5~1 秒,
# 见 docs/design/voice-companion/03-phase2-实现记录.md)。去 bailian.console.aliyun.com
# 开通拿 key,填到 .env 的 DASHSCOPE_API_KEY。留空则语音按钮报错提示未配置。
DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "").strip()
DASHSCOPE_STT_MODEL: str = (
    os.environ.get("DASHSCOPE_STT_MODEL", "qwen3-asr-flash").strip() or "qwen3-asr-flash"
)
# 语音合成(TTS)本体:同样是阿里 DashScope,复用同一个 DASHSCOPE_API_KEY(2026-07-09
# 从 edge-tts 切过来——那是扒微软 Edge 浏览器内部接口的非官方库,没有 SLA,合成失败
# /超时正是"语音伴聊没声音"投诉的主因;qwen3-tts-flash 走跟 STT 完全一样的
# multimodal-generation/generation 端点和鉴权,官方产品线,稳定性有保障)。
DASHSCOPE_TTS_MODEL: str = (
    os.environ.get("DASHSCOPE_TTS_MODEL", "qwen3-tts-flash").strip() or "qwen3-tts-flash"
)
# 图片转文字(VL)本体:同样是阿里 DashScope,复用同一个 DASHSCOPE_API_KEY。
# 用途:主模型不支持视觉时(如 DeepSeek),先用 qwen-vl 把图片读成文字描述再喂给
# 主模型(见 core/vision.py)。qwen-vl-max 能力最强(复杂图表/截图小字),单图成本
# 不到一分钱;嫌贵可改 qwen-vl-plus,只是复杂图表细节会丢一些。
DASHSCOPE_VL_MODEL: str = (
    os.environ.get("DASHSCOPE_VL_MODEL", "qwen-vl-max").strip() or "qwen-vl-max"
)

# 清洗步骤仍走 SiliconFlow(跟识别本体是两个不同服务商,互不影响)。
# 去 siliconflow.cn 注册拿免费 key,填到 .env 的 SILICONFLOW_API_KEY。
STT_API_KEY: str = os.environ.get("SILICONFLOW_API_KEY", "").strip()
STT_BASE_URL: str = (
    os.environ.get("STT_BASE_URL", "https://api.siliconflow.cn/v1").strip()
    or "https://api.siliconflow.cn/v1"
)

# 转写完的逐字稿常带口癖(呃/然后/就是)、同音字错误、被音译错的中英混说专名,
# 转写后再过一遍 LLM 清洗。复用 STT_API_KEY/STT_BASE_URL(同一个 SiliconFlow 账号),
# 换成 chat/completions。实测 7B 小模型修不对这类音译错误,72B 才能稳定纠出
# "OpenAI Codex"这类词——故不用免费小模型。
STT_CLEANUP_ENABLED: bool = _parse_bool(os.environ.get("STT_CLEANUP_ENABLED", ""), True)
STT_CLEANUP_MODEL: str = (
    os.environ.get("STT_CLEANUP_MODEL", "Qwen/Qwen2.5-72B-Instruct").strip()
    or "Qwen/Qwen2.5-72B-Instruct"
)

# === 语音伴聊模式(实验性,见 docs/design/voice-companion/)===
# 手机上像打电话一样跟 AI 说话。默认开;体验不好会整体移除,故独立成 VOICE_ 前缀。
VOICE_ENABLED: bool = _parse_bool(os.environ.get("VOICE_ENABLED", ""), True)
# 音色:qwen3-tts-flash 的音色名(如 Cherry/Ethan/Serena),不是 edge-tts 的
# "zh-CN-XiaoxiaoNeural" 这种命名法,见 https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list
VOICE_TTS_VOICE: str = (
    os.environ.get("VOICE_TTS_VOICE", "Cherry").strip() or "Cherry"
)
# === 后台任务引擎(core/task_runner.py,原 P1 语音任务板;2026-07-29 通用化:
# 语音派发/cron 定时/普通会话发起「独立新会话」三种触发方式共用同一个引擎,
# 不再是语音专属,故常量名去掉 VOICE_ 前缀)===
# 并发上限 / 单任务超时(分钟)。并发上限 2026-08-04 主人确认由 3 放开到 7
# (原定 3 是为了防止并发任务打爆 API 额度,先跑一个月看看额度压力再定)
TASK_MAX_CONCURRENCY: int = int(os.environ.get("TASK_MAX_CONCURRENCY", "7"))
TASK_TIMEOUT_MIN: int = int(os.environ.get("TASK_TIMEOUT_MIN", "30"))
# 单轮 agentic 轮数上限,0=跟随全局 MAX_TURNS(全局也是 0 即不限,由 TASK_TIMEOUT_MIN
# 超时兜底)。保留独立开关是因为查日志/翻代码这类任务动辄几十轮,2026-07-10 真机
# 事故:全局 40 轮让一个查日志任务白跑 8 分钟。
TASK_MAX_TURNS: int = int(os.environ.get("TASK_MAX_TURNS", "0"))
# 派活判断目前完全靠模型自己读【派活规则】临场判断,没有代码兜底——真机复盘过
# 一次长任务(7步骤的复杂指令)险些没被当成后台任务处理(见 2026-07-09 事故复盘)。
# 这里加一道低成本兜底:识别文本超过这个字数,就在 prompt 里额外加一句强提示,
# 而不是代码直接绕过模型硬派——派活前还要判断方向是否需要先跟用户确认(见
# prompts.py【派活规则】第1条),这一步不能被代码抢走。日常聊天/问答基本都在
# 20字以内,超过这个阈值大概率是交代复杂事情,而不是随口一句话。
VOICE_LONG_TASK_CHARS: int = int(os.environ.get("VOICE_LONG_TASK_CHARS", "50"))

# P3:端到端语音进语音出模型(见 voice/omni_realtime.py)。P2 自建全双工管线
# (ws.py + DashScope 实时识别 WS)已于 2026-07-11 整体退休,见
# docs/adr/0004-voice-omni-only.md;其判定纯函数与调优终值存档在
# docs/design/voice-companion/04-echo-heuristics-archive.md(2026-07-23 从活代码
# 降级为文档——原 voice/heuristics.py 零调用方)。已用真实账号连线验证过
# session.update/function calling 全流程可用。
VOICE_OMNI_REALTIME_MODEL: str = (
    os.environ.get("VOICE_OMNI_REALTIME_MODEL", "qwen3.5-omni-flash-realtime").strip()
    or "qwen3.5-omni-flash-realtime"
)
# 阶段二(前端 WebRTC)专用:WebRTC 的 SDP 信令交换端点跟上面 WS 用的全局域名不是
# 一回事,必须是"{WorkspaceId}.cn-beijing.maas.aliyuncs.com"这种工作区专属域名
# (2026-07-10 真机连线验证过,全局域名对这个路径直接 404)。去百炼控制台右上角
# 用户图标弹窗里复制"业务空间ID"填这里,不是什么敏感凭证但仍按环境变量走,别写死。
VOICE_OMNI_WORKSPACE_ID: str = os.environ.get("VOICE_OMNI_WORKSPACE_ID", "").strip()
# 免提通话开关:开了之后通话视图(#callView)走 Omni-Realtime 的 WebRTC 连线。
# 关着(或登录时 /voice/config 预取失败)则免提不可用,前端回落按住说话——
# P2 自建 WS 链路已删,这个开关不再是"两条链路二选一",而是"免提有无"。
VOICE_OMNI_ENABLED: bool = _parse_bool(os.environ.get("VOICE_OMNI_ENABLED", ""), False)
# Omni 出声模式的音色——跟 VOICE_TTS_VOICE(Qwen-TTS 用)是两张不同的音色表,
# 不能混用:2026-07-10 真机实锤 Cherry 在 qwen3.5-omni-flash-realtime 上直接
# 400 InvalidParameter,每轮回复全灭。音色表见百炼文档 omni-voice-list,
# Serena(苏瑶)是最接近 Cherry 的温柔女声。
VOICE_OMNI_VOICE: str = os.environ.get("VOICE_OMNI_VOICE", "Serena").strip() or "Serena"
# Omni WebRTC 链路 turn_detection 的灵敏度(0.0-1.0,越低越灵敏)。真机在有环境
# 噪音的房间里 0.0→0.3→0.5→0.7 调了三轮(呼吸声/杂音被当成开口,还被识别模型
# 硬编成"嗯/哦"这类语气词),0.7 是用户明确要求"大幅提升"后的终值,不用再调。
VOICE_VAD_THRESHOLD: float = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.7"))
# Omni WebRTC 链路的静音判停时长——2026-07-10 真机反馈 1500ms 对有停顿思考习惯
# ("呃"、组织语言)的说话方式太短,句子中间的正常停顿就被当成说完了,3000ms 后
# 2026-07-12 真机仍反馈整句话被拆成好几个请求发给 Claude(semantic_vad 语义判停
# 本身也会提前拍板),拉长到 4500ms 牺牲了打断响应速度。
# 2026-07-22 上午改成 800ms(用户反馈 4500ms 等太久),当晚真机对话全面错位:800ms
# 让正常语流停顿(500~1500ms)都触发判停,一句话被高频腰斩成多段,叠加前端缓冲
# 定时器被 speech 事件误覆盖的 bug,表现为消息被吞/答非所问。回调到 2500ms 折中:
# semantic_vad 语义判停为主,2500ms 静音兜底,比 4500ms 快很多又不至于句中误切。
VOICE_OMNI_VAD_SILENCE_MS: int = int(os.environ.get("VOICE_OMNI_VAD_SILENCE_MS", "2500"))

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

    `voice-chat:` 是语音模块保留前缀(主语音通话固定键)。`task:` 是统一后台任务
    引擎(core/task_runner.py)的前缀——语音派发、cron 定时、普通会话发起"独立
    新会话"三种触发方式共用同一套 task 表和 session_key 命名(2026-07-29 统一,
    以前 voice-task:/cron-task: 是两个各自为政的前缀,现在合一,靠 tasks 表的
    origin 字段区分谁触发的)。这些都已经是完整 key,原样透传不再套 "web:" 前缀
    ——这样侧边栏"后台任务"分组里的会话可以直接用 index.html 现成的
    openConv()/发消息面板打开,不用专门另写一套路由逻辑。这些前缀是保留字,
    不会跟现有项目哈希形态的 conv_id 冲突。
    """
    key = _resolve_session_key_impl(platform, chat_id)
    if IS_SERVER:
        # server 模式所有会话 key 带租户前缀 t:<tid>: —— 物理隔离(per-tenant
        # state.db)之上的第二道逻辑防线:即便某条路径误用了别租户的连接,key
        # 前缀也对不上,查不到数据。tenancy 依赖本模块,只能函数内延迟 import。
        from .tenancy import context as tenant_context

        return f"t:{tenant_context.current()}:{key}"
    return key


def _resolve_session_key_impl(platform: str, chat_id: object) -> str:
    if platform == "telegram" and isinstance(chat_id, int) and chat_id < 0:
        return f"tg:{chat_id}"
    if platform == "web":
        if chat_id == "main":
            return SESSION_KEY
        if isinstance(chat_id, str) and (
            chat_id.startswith("voice-chat:") or chat_id.startswith("task:")
        ):
            return chat_id
        return f"web:{chat_id}"
    return SESSION_KEY if UNIFY_SESSIONS else f"{platform}:{chat_id}"


def strip_tenant_prefix(session_key: str) -> str:
    """剥掉 server 模式 resolve_session_key 加的 't:<tid>:' 前缀;非该形态原样返回。

    所有按 'web:'/'tg:'/'task:' 等前缀判断会话类型的代码,判断前先过这道
    (personal 模式的 key 永远不带 t: 前缀,过它也是原样返回,零副作用)。
    """
    if session_key.startswith("t:"):
        parts = session_key.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[2]
    return session_key


def project_hash_from_key(session_key: str) -> str | None:
    """从会话 key 里取项目哈希;非项目会话返回 None。

    兼容两种形态:
    - web:p<hash>:<slug> —— 正式项目会话(改名前的存量 key)
    - web:local-p<hash>:<slug> —— 前端草稿会话(改名时引入 local- 前缀,
      见 index.html newChatIn;发出首条消息后后端仍原样落库,key 不带去前缀)
    其余(main/默认项目的老 web:<conv>/TG/CLI/task:)都不带项目哈希。
    server 模式没有「绑本地仓库的项目」概念(客户会话跑在租户沙箱),直接 None。
    """
    if IS_SERVER:
        return None
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "web":
        head = parts[1]
        if head.startswith("p") and len(head) > 1:
            return head[1:]
        if head.startswith("local-p") and len(head) > len("local-p"):
            return head[len("local-p"):]
    return None


def project_root_for(session_key: str) -> str | None:
    """项目会话绑定的仓库根目录(不含 worktree);非项目会话返回 None。

    反查走 session_store 的哈希→路径 映射表(延迟 import 打破 config↔store
    循环依赖)。
    """
    h = project_hash_from_key(session_key)
    if h is None:
        return None
    from .memory import session_store

    return session_store.path_for_hash(h)


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


# === server 模式派生覆盖(放在所有常量定义之后,统一收口;personal 模式此块整体不生效)===
# 原则:server 的默认值在这里一次改完,业务代码不各自判断。这些覆盖的是「多租户对外
# 服务下不成立」的本地假设——语音(全局单份 key+高成本)、跨入口统一会话(没有「主人
# 一个大脑」)、租户注册 stdio MCP(=远程 RCE 第二条路,永禁)。
if IS_SERVER:
    VOICE_ENABLED = False
    UNIFY_SESSIONS = False
    WEB_ALLOW_STDIO_MCP = False  # 忽略 env,强制 fail-closed
    # 多租户抢保温池:缩短空闲回收(可在 .env 用 CLIENT_POOL_IDLE_TTL 显式覆盖回来)
    if "CLIENT_POOL_IDLE_TTL" not in os.environ:
        CLIENT_POOL_IDLE_TTL = 120
    # 数据目录下按租户分根:data/tenants/<tid>/{state.db,brain/,workspace/,images/,audio/}
    TENANTS_DIR: Path = DATA_DIR / "tenants"


# === 收敛 secret 暴露面 ===
def _scrub_env_secrets() -> None:
    """把 vococo 自用的 secret 从 os.environ 移除。

    背景:SDK 用 os.environ 当 Claude Code CLI 子进程的基底 env,而 load_dotenv 已把整个
    .env 灌进了 os.environ —— 于是订阅 token / bot token / STT key / VAPID 私钥 / Web 口令
    全都躺在 Bash 工具能 `echo $VAR` 读到的环境里,一次 prompt injection 即可外带。
    (实测:pop 前子进程 echo 得到 108/46/24 位;pop 后全为 0。)

    这些值上面都已读进本模块常量,运行时全走 config.X 而非 os.environ,故从环境移除是安全的:
    - 订阅 token:实测 CLI 用自己存的凭据(claude setup-token)鉴权,移除后请求照常成功。
    - 第三方 provider key【不在此列】:它来自设置页手动配置的 web_providers、每轮经 options.env
      注入,CLI 必须拿它调第三方端点,无法移除 —— 那条只能靠 danger.py 定向拦截 + 沙箱根治。
    注:这是收窄「被动 env 泄露」,不是硬边界(同用户 Bash 仍可 `cat .env`);根治需 Tier3 沙箱。
    设 VOCOCO_KEEP_ENV_SECRETS=1 可跳过本清理(排障用)。
    """
    if _parse_bool(os.environ.get("VOCOCO_KEEP_ENV_SECRETS", ""), False):
        return
    for _k in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SILICONFLOW_API_KEY",
        "VAPID_PRIVATE_KEY",
        "WEB_AUTH_TOKEN",
    ):
        os.environ.pop(_k, None)


_scrub_env_secrets()
