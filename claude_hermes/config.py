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
# 助理主人的称呼:注入 PERSONA / 工具描述,让助理知道在为谁服务。
# 开源默认「主人」,在 .env 设 HERMES_USER_NAME=你的名字 即个性化。
USER_NAME: str = os.environ.get("HERMES_USER_NAME", "主人").strip() or "主人"
MODEL: str = os.environ.get("AGENT_MODEL", "claude-sonnet-5").strip()
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "100"))
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

# 运行时数据目录(prompt_toolkit 历史等)
DATA_DIR: Path = _ROOT / "data"
# 用户发的图片落盘目录(Web 端消息里的图片,持久化后刷新页面仍可见)
IMAGES_DIR: Path = DATA_DIR / "images"

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
# P1 任务板:后台任务并发上限 / 单任务超时(分钟)/ 完成播报档位(idle=等空闲插播,silent=只更新卡片)
VOICE_TASK_MAX_CONCURRENCY: int = int(os.environ.get("VOICE_TASK_MAX_CONCURRENCY", "3"))
VOICE_TASK_TIMEOUT_MIN: int = int(os.environ.get("VOICE_TASK_TIMEOUT_MIN", "30"))
VOICE_ANNOUNCE: str = os.environ.get("VOICE_ANNOUNCE", "idle").strip().lower() or "idle"
# 派活判断目前完全靠模型自己读【派活规则】临场判断,没有代码兜底——真机复盘过
# 一次长任务(7步骤的复杂指令)险些没被当成后台任务处理(见 2026-07-09 事故复盘)。
# 这里加一道低成本兜底:识别文本超过这个字数,就在 prompt 里额外加一句强提示,
# 而不是代码直接绕过模型硬派——派活前还要判断方向是否需要先跟用户确认(见
# prompts.py【派活规则】第1条),这一步不能被代码抢走。日常聊天/问答基本都在
# 20字以内,超过这个阈值大概率是交代复杂事情,而不是随口一句话。
VOICE_LONG_TASK_CHARS: int = int(os.environ.get("VOICE_LONG_TASK_CHARS", "50"))

# P2 全双工:/voice/ws 免提连续对话开关(独立开关,方便单独回退到按住说话)。
VOICE_WS_ENABLED: bool = _parse_bool(os.environ.get("VOICE_WS_ENABLED", ""), True)
# 假打断回滚超时:打断触发后这么久还没等到有效转写,就当误触发,撤销截断、恢复播放。
# 2026-07-08 从 1500 调到 8000:改成 DashScope 实时 WS 之后,这个窗口要覆盖的
# 不再是"客户端本地 VAD 已经判完、只等一个网络包"那么短的时间,而是"用户真的
# 把这句话说完 + DashScope 转写出结果"的完整耗时——真机测试中一句 4-5 秒的话
# 配合上游网络往返,1.5 秒经常等不到 completed 就被误判成假打断、白白撤销了
# 一次正常的追问,见 03-phase2-实现记录.md。
VOICE_FALSE_POSITIVE_TIMEOUT_MS: int = int(
    os.environ.get("VOICE_FALSE_POSITIVE_TIMEOUT_MS", "8000")
)
# 2026-07-08:免提识别改用 DashScope 实时语音 WS(server_vad),替换掉真机实测
# 不可靠的客户端 ML VAD——服务端专业断句判断,不用客户端自己猜"说完了没"。
# threshold/silence_duration_ms 是 DashScope 那边 server_vad 的调优参数。
# threshold 越低越灵敏——最早为了验证协议图省事调成了 0.0(比 DashScope 自己
# 的默认值 0.2 还灵敏),真机在有环境噪音的房间里实测明显太敏感:呼吸声/环境
# 杂音被当成开口,还被识别模型强行转写成"嗯/哦/是的"这类语气词(模型倾向于
# 给含糊音频编一个说得过去的短词,而不是老实返回空)。0.0→0.3→0.5/500→600ms
# 调了两轮,真机反馈仍然偏灵敏,大幅提到 0.7(逼近上限 1.0)/1000ms——用户
# 明确要求两个都"大幅提升"。副作用:开口打断的响应会更慢、对轻声/尾音的
# 识别可能变迟钝,真机测试若矫枉过正需要往回收,见
# docs/design/voice-companion/03-phase2-实现记录.md。threshold 加上声纹识别
# (见 voiceprint.py)配合下来真机反馈已经不错(环境噪音基本不再被误识别成
# 语气词了),threshold 不用再调;silence_duration_ms 继续从 1000→1500,
# 减少"一段完整的话中间停顿稍长就被切成好几句"的情况——代价同上,停顿判定
# 更久意味着说完到系统反应过来的间隔更长一点。
DASHSCOPE_REALTIME_MODEL: str = (
    os.environ.get("DASHSCOPE_REALTIME_MODEL", "qwen3-asr-flash-realtime").strip()
    or "qwen3-asr-flash-realtime"
)
# 2026-07-10 P3 阶段一:端到端语音进语音出模型(用来替换"自己识别+自己合成+自己
# 拼播放队列"这条今天连炸三次故障的自建链路,见 voice/omni_realtime.py),跟上面
# DASHSCOPE_REALTIME_MODEL(纯识别,ws.py 在用)是两个不同用途的模型,不要混用。
# 已用真实账号连线验证过 session.update/function calling 全流程可用。
VOICE_OMNI_REALTIME_MODEL: str = (
    os.environ.get("VOICE_OMNI_REALTIME_MODEL", "qwen3.5-omni-flash-realtime").strip()
    or "qwen3.5-omni-flash-realtime"
)
# 阶段二(前端 WebRTC)专用:WebRTC 的 SDP 信令交换端点跟上面 WS 用的全局域名不是
# 一回事,必须是"{WorkspaceId}.cn-beijing.maas.aliyuncs.com"这种工作区专属域名
# (2026-07-10 真机连线验证过,全局域名对这个路径直接 404)。去百炼控制台右上角
# 用户图标弹窗里复制"业务空间ID"填这里,不是什么敏感凭证但仍按环境变量走,别写死。
VOICE_OMNI_WORKSPACE_ID: str = os.environ.get("VOICE_OMNI_WORKSPACE_ID", "").strip()
VOICE_VAD_THRESHOLD: float = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.7"))
VOICE_VAD_SILENCE_MS: int = int(os.environ.get("VOICE_VAD_SILENCE_MS", "1500"))
# 回声兜底:AI 自己的声音从手机扬声器漏回麦克风,DashScope 会把它当成真实
# 用户开口识别出来,导致 AI 打断自己形成死循环——echoCancellation 只是缓解不是
# 根治(见 03-phase2-实现记录.md)。打断转写出来的内容如果跟 AI 刚说的话高度
# 重合(containment ratio,见 voice/ws.py 的 looks_like_self_echo),就当是回声、
# 不当真打断处理。阈值凭经验给的默认值,真机环境不同需要重新调。
VOICE_SELF_ECHO_THRESHOLD: float = float(os.environ.get("VOICE_SELF_ECHO_THRESHOLD", "0.6"))
# 2026-07-08:上面这条回声兜底原来只在"正在打断一个还没说完的回答"时生效——
# 真机反馈偶发录到 AI 自己的尾音,根因是 AI 说完最后一句、服务端状态已经翻回
# idle 后,客户端音箱可能还在把最后一两句播完,这段尾音漏回麦克风时早就不在
# "打断"场景里了,原来的回声判断整个被跳过。这里加一个"刚说完话之后的宽限期"
# (毫秒),这段时间内即便状态已经是 idle,新转写内容如果跟"刚刚这一轮说的话"
# 高度重合,也当回声处理、不当真开新一轮。默认给一个短窗口,覆盖典型的尾部
# 播放延迟就够;调太大会有把用户真提的、恰好和上一轮用词很像的追问也误伤的风险。
VOICE_POST_DONE_ECHO_GUARD_MS: int = int(
    os.environ.get("VOICE_POST_DONE_ECHO_GUARD_MS", "1500")
)

# 2026-07-09:呼吸声/环境杂音时长太短,即便 DashScope 硬编出一个语气词,也不该
# 当成一句真话——这一条不看转写文字,只看物理时长,所以在空闲状态也生效
# (looks_like_filler_only 那条只在打断场景生效,见上面的说明)。
# 判断依据是 speech_started→speech_stopped 的原始时间差(见 voice/ws.py 的
# estimate_speech_too_short)——原计划减掉 VOICE_VAD_SILENCE_MS 判定静音尾巴,
# 但真机实测发现这段原始时间差经常比配置的静音时长还短(DashScope 实际判"说完
# 了"用的时长跟我们配置的对不上),减法会算出负数、把正常短句全部误杀,已改成
# 直接用原始时间差判断,不再依赖那个不成立的假设。默认给一个保守的下限,只挡
# 真正瞬时(几十毫秒级)的噪声;调太大会把用户真说的很短的字也当噪声吃掉。
VOICE_MIN_SPEECH_MS: int = int(os.environ.get("VOICE_MIN_SPEECH_MS", "250"))

# 2026-07-08:声纹识别——分清"这是本人在说话"还是"背景里别人在说话"。
# 免提场景背景有人说话时,普通降噪分不出"哪个人声是你",这块专门加了目标
# 说话人识别(见 voice/voiceprint.py)。异步、不卡对话速度(见实现文档"方案
# B")——转写完立刻正常起一轮,声纹比对在后台并行跑,判定"不像是你"再把这
# 一轮撤回。默认开启,体验不好可以关掉退回没有声纹这层的行为。
VOICE_VOICEPRINT_ENABLED: bool = _parse_bool(os.environ.get("VOICE_VOICEPRINT_ENABLED", ""), True)
# 余弦相似度阈值:新样本跟声纹参照质心的相似度低于这个值,判定"不是本人"。
# 凭经验给的起点,没有真实用户音频做过校准,真机用下来大概率需要重新调
# (太低会漏过背景说话人,太高会把本人的正常语气变化也误判成"不是你")。
VOICE_VOICEPRINT_MATCH_THRESHOLD: float = float(
    os.environ.get("VOICE_VOICEPRINT_MATCH_THRESHOLD", "0.75")
)
# 声纹参照样本数少于这个值时,只用来建立参照、不做拦截判定(冷启动阶段,
# 参照本身还不可信,不能拿来筛掉任何人)。
VOICE_VOICEPRINT_MIN_SAMPLES: int = int(
    os.environ.get("VOICE_VOICEPRINT_MIN_SAMPLES", "3")
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

    `voice-chat:`/`voice-task:` 是语音模块保留的前缀(主语音通话、后台任务各一个
    固定/派生键),已经是完整 key,原样透传不再套 "web:" 前缀——这样侧边栏"语音
    任务"分组里的会话可以直接用 index.html 现成的 openConv()/发消息面板打开,
    不用给语音专门另写一套路由逻辑。这两个前缀是新引入的保留字,不会跟现有项目
    哈希形态的 conv_id 冲突。
    """
    if platform == "telegram" and isinstance(chat_id, int) and chat_id < 0:
        return f"tg:{chat_id}"
    if platform == "web":
        if chat_id == "main":
            return SESSION_KEY
        if isinstance(chat_id, str) and (
            chat_id.startswith("voice-chat:") or chat_id.startswith("voice-task:")
        ):
            return chat_id
        return f"web:{chat_id}"
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


# === 收敛 secret 暴露面 ===
def _scrub_env_secrets() -> None:
    """把 hermes 自用的 secret 从 os.environ 移除。

    背景:SDK 用 os.environ 当 Claude Code CLI 子进程的基底 env,而 load_dotenv 已把整个
    .env 灌进了 os.environ —— 于是订阅 token / bot token / STT key / VAPID 私钥 / Web 口令
    全都躺在 Bash 工具能 `echo $VAR` 读到的环境里,一次 prompt injection 即可外带。
    (实测:pop 前子进程 echo 得到 108/46/24 位;pop 后全为 0。)

    这些值上面都已读进本模块常量,运行时全走 config.X 而非 os.environ,故从环境移除是安全的:
    - 订阅 token:实测 CLI 用自己存的凭据(claude setup-token)鉴权,移除后请求照常成功。
    - 第三方 provider key【不在此列】:它来自 ~/.claude-hermes/config.yaml、每轮经 options.env
      注入,CLI 必须拿它调第三方端点,无法移除 —— 那条只能靠 danger.py 定向拦截 + 沙箱根治。
    注:这是收窄「被动 env 泄露」,不是硬边界(同用户 Bash 仍可 `cat .env`);根治需 Tier3 沙箱。
    设 HERMES_KEEP_ENV_SECRETS=1 可跳过本清理(排障用)。
    """
    if _parse_bool(os.environ.get("HERMES_KEEP_ENV_SECRETS", ""), False):
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
