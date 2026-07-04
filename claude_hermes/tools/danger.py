"""危险命令拦截 —— PreToolUse hook,拦下真正毁灭性的 Bash 命令。

claude-hermes 默认 bypassPermissions(工具自动执行),方便但有风险。这里加一道
**保守的安全网**:只拦真正会造成灾难的命令(删整个根/家目录、格式化磁盘、覆写裸盘、
fork 炸弹等),宁可漏放也别误伤日常操作——`rm -rf ./build` 这种子目录删除【不拦】。

被拦时返回 permissionDecision=deny,agent 收到拒绝原因,可改用更安全的方式或让用户手动执行。
想关掉:.env 里 DANGER_GUARD=0。

除「灾难级 → 直接拦」外,这里还做一层【审批闸】(APPROVAL_GATE,默认开):对
「危险但非灾难」的 5 类操作 —— 写工作目录外的文件、git push / reset --hard、rm -rf、
包安装(pip/npm/brew…)、curl|sh —— 在【有交互通道时】弹按钮请用户批准,无通道则放行。
这就是「远程编码:动手前对齐」的安全阀,复刻 Claude Code 的权限体验但适配手机低摩擦。
"""
from __future__ import annotations

import contextvars
import os
import re

from .. import config

try:  # HookMatcher 用于把本 hook 挂进 SDK;旧 SDK 缺失则优雅降级(不挂 hook)
    from claude_agent_sdk import HookMatcher
except Exception:  # pragma: no cover
    HookMatcher = None

# 灾难目标:整个根 / 整个家目录 / 根通配(而非某个子目录)
_CATASTROPHIC_TARGET = re.compile(r"(^|\s)(/|/\*|~|~/|\$HOME|\$HOME/|\*)(\s|$)")

# 其余灾难级模式(literal)
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(disk|sd|nvme|rdisk)"), "dd 写裸磁盘"),
    (re.compile(r">\s*/dev/(sd|nvme|disk|rdisk)"), "覆写裸磁盘"),
    (re.compile(r":\s*\(\s*\)\s*\{.*[|&].*\}\s*;"), "fork 炸弹"),
    (re.compile(r"\bchmod\s+-R\s+0*777\s+/(\s|$)"), "chmod -R 777 根目录"),
]


def _rm_is_catastrophic(cmd: str) -> bool:
    """rm + 递归标志 + 灾难目标(整根/整家目录/根通配)才算灾难;删子目录不算。"""
    if not re.search(r"\brm\s+(-\S*[rR])", cmd):  # 必须带递归
        return False
    # 去掉 rm 及其后的选项,只看目标部分是否命中灾难目标
    return bool(_CATASTROPHIC_TARGET.search(cmd))


def is_dangerous(command: str) -> str | None:
    """返回命中的危险说明,安全则返回 None。"""
    c = (command or "").strip()
    if not c:
        return None
    if _rm_is_catastrophic(c):
        return "rm -r 删整个根/家目录"
    for rx, why in _PATTERNS:
        if rx.search(c):
            return why
    return None


async def pretool_danger_hook(input_data, tool_use_id, context):
    """PreToolUse hook:对 Bash 命令做危险拦截。返回 deny 则该次工具调用被拒。"""
    try:
        if input_data.get("tool_name") != "Bash":
            return {}
        cmd = (input_data.get("tool_input") or {}).get("command", "")
        why = is_dangerous(cmd)
        if why:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"⛔ 危险命令被 Hermes 拦截({why})。如确需执行,请你手动在终端运行,"
                        "或改用更安全的方式。"
                    ),
                }
            }
    except Exception:
        pass  # hook 出错不阻断正常流程
    return {}


# ── 审批闸:5 类「危险但非灾难」操作 → 有交互通道时请用户批准 ──────────────────
# 都是命令级(Bash)的模式;「写工作目录外文件」是文件级,单独在 classify 里判。
_ESCALATE_BASH: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgit\s+push\b"), "git push(推送到远端,对外)"),
    (re.compile(r"\bgit\s+reset\b[^\n]*--hard\b"), "git reset --hard(丢弃改动,不可逆)"),
    (re.compile(r"\brm\s+-\S*[rR]"), "rm -rf(递归删除)"),
    (
        re.compile(
            r"\b(pip3?|pipx|uv|npm|pnpm|yarn|brew|apt|apt-get|gem|cargo|go)\s+"
            r"(install|add|get|i)\b"
        ),
        "包安装(改动环境)",
    ),
    (re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), "curl|sh(下载执行,供应链风险)"),
]

# 会改写文件系统的工具 → 检查目标是否落在工作目录外
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# 当前轮的工作目录(工作目录功能会在开轮时 set_cwd;未设则 None → 越界检查休眠)
_cwd_var: contextvars.ContextVar = contextvars.ContextVar("hermes_agent_cwd", default=None)


def set_cwd(path: str | None) -> contextvars.Token:
    """开轮时登记本轮工作目录(供越界写检测)。converse 每轮调用,随 contextvar 传进 hook。"""
    return _cwd_var.set(path or None)


def reset_cwd(token: contextvars.Token) -> None:
    try:
        _cwd_var.reset(token)
    except (ValueError, LookupError):
        pass


def current_cwd() -> str | None:
    return _cwd_var.get()


def _outside_cwd(path: str, cwd: str | None) -> bool:
    """目标文件是否落在 cwd 之外(含符号链接解析)。cwd 为空则不判(休眠)。"""
    if not cwd or not path:
        return False
    try:
        target = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        base = os.path.realpath(cwd)
        return os.path.commonpath([target, base]) != base
    except (ValueError, OSError):
        return False


def classify(tool_name: str, tool_input: dict, cwd: str | None = None) -> tuple[str, str]:
    """把一次工具调用分成 allow / escalate / block,附一句原因。

    - block:灾难级(删根/格式化…),直接拦。
    - escalate:5 类危险操作,请用户批准。
    - allow:其余,放行。
    """
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = ti.get("command", "") or ""
        why = is_dangerous(cmd)
        if why:
            return ("block", why)
        for rx, reason in _ESCALATE_BASH:
            if rx.search(cmd):
                return ("escalate", reason)
        return ("allow", "")
    if tool_name in _WRITE_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if _outside_cwd(path, cwd):
            return ("escalate", f"写工作目录外的文件({path})")
    return ("allow", "")


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ── 强制前台执行:拦下 run_in_background,引导模型改前台重试 ────────────────────
# 本 harness 每轮用一个 ClaudeSDKClient,收到【本轮 ResultMessage】就退出 receive_response
# 并关闭子进程。而 run_in_background 的子代理/命令是「立即返回、真正干活排到 ResultMessage
# 之后以 task_* 系统消息陆续上报」—— 届时子进程已被关掉,任务被腰斩:模型嘴上说「已在后台
# 发起」,实际一步没跑,也没有任何结果/显示。
# 修法:PreToolUse hook 检测到 run_in_background=true 就 deny,并在原因里明确要求【去掉该
# 参数、以前台(同步)方式重新调用】。deny 是 hook 的硬拦截(危险命令拦截同款,bypassPermissions
# 下也生效);相比 updatedInput 改写入参(疑似在 bypass 模式被 CLI 忽略),deny 更可靠。
# 模型重试为前台后,子代理会在本轮内跑完并靠既有子代理卡片实时显示。
# 子代理工具新版叫 Agent、老版叫 Task,两者都收;Bash 也有 run_in_background。
_BACKGROUNDABLE = {"Agent", "Task", "Bash"}


def _wants_background(tool_name: str, tool_input: dict) -> bool:
    """该工具调用是否请求了后台执行(run_in_background=true)。"""
    if tool_name not in _BACKGROUNDABLE:
        return False
    return bool((tool_input or {}).get("run_in_background"))


def _deny_background(tool_name: str) -> dict:
    return _deny(
        f"🚫 本 harness【不支持后台任务】(run_in_background):后台任务会在本轮结束时被中断、"
        f"永远跑不完,也没有任何结果。请【去掉 run_in_background 参数】,以【前台(同步)】方式"
        f"重新调用 {tool_name}——前台子代理会在本轮内跑完并实时显示进度。"
    )


def _hook_debug(msg: str) -> None:
    """临时诊断:把 hook 的关键决策落到独立文件,便于排查「改动是否生效」。确认后可删。"""
    try:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                         "data", "logs", "hook_debug.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _describe(tool_name: str, tool_input: dict) -> str:
    """给审批弹窗一行「具体要干什么」。"""
    ti = tool_input or {}
    if tool_name == "Bash":
        return f"`{(ti.get('command') or '').strip()[:200]}`"
    if tool_name in _WRITE_TOOLS:
        return f"→ {ti.get('file_path') or ti.get('notebook_path') or '?'}"
    return tool_name


async def _ask_approval(tool_name: str, reason: str, tool_input: dict) -> bool:
    """有交互通道 → 弹「允许一次 / 拒绝」按钮并阻塞等;无通道 → 放行(信任该通道)。

    复用 ask_user 同款 clarify 机制:回复经网关「拿锁前 resolve」解除,不会死锁。
    超时 / 发送失败 → 视为拒绝(危险操作宁可不做)。
    """
    from ..gateway import clarify
    from ..gateway.core import Choice

    ctx = clarify.current()
    if ctx is None:
        return True  # 非交互(CLI/eval/cron):无人可问,放行
    p = clarify.register(ctx.session_key, ["允许一次", "拒绝"])
    try:
        opts = [
            (f"/clarify {p.clarify_id} 0", "✅ 允许一次"),
            (f"/clarify {p.clarify_id} 1", "🛑 拒绝"),
        ]
        prompt = f"⚠️ 需要批准:{reason}\n{_describe(tool_name, tool_input)}"
        await ctx.adapter.present_choice(ctx.chat_id, Choice(prompt=prompt, options=opts))
    except Exception:
        clarify.resolve(p.clarify_id, "拒绝")
        return False
    answer = await clarify.wait(p.clarify_id, config.CLARIFY_TIMEOUT)
    return answer == "允许一次"


async def pretool_guard_hook(input_data, tool_use_id, context):
    """PreToolUse hook:灾难级 → 拦;危险级 → 请批准;其余放行。

    这是接进 SDK 的那个 hook(build_hooks)。旧的 pretool_danger_hook 只做灾难拦截,
    保留供既有测试;新逻辑在此,由 DANGER_GUARD / APPROVAL_GATE 两开关分别控制。
    """
    try:
        tool_name = input_data.get("tool_name", "") or ""
        tool_input = input_data.get("tool_input") or {}
        bg = _wants_background(tool_name, tool_input)
        _hook_debug(f"[hook] tool={tool_name} run_in_background={bg}")
        # 后台任务:直接 deny 引导前台重试(见 _wants_background 上方说明),优先于其余判定
        if bg:
            _hook_debug(f"[hook] DENY background -> {tool_name}")
            return _deny_background(tool_name)
        verdict, reason = classify(tool_name, tool_input, cwd=current_cwd())
        if verdict == "block" and config.DANGER_GUARD:
            return _deny(
                f"⛔ 危险命令被 Hermes 拦截({reason})。如确需执行,请你手动在终端运行。"
            )
        if verdict == "escalate" and config.APPROVAL_GATE:
            if not await _ask_approval(tool_name, reason, tool_input):
                return _deny(
                    f"🛑 你未批准此操作({reason})。已跳过;如需执行请手动运行或改用更安全方式。"
                )
    except Exception:
        pass  # hook 出错绝不阻断正常流程(宁可放行也别把 agent 卡死)
    return {}


def build_hooks() -> dict | None:
    """返回挂给 ClaudeAgentOptions.hooks 的结构;SDK 不支持则 None。

    始终挂 PreToolUse:除危险拦截/审批闸(各由 DANGER_GUARD/APPROVAL_GATE 开关控制)外,
    还负责拦下 run_in_background、引导模型改前台重试——这是纠正「后台任务被腰斩」的正确性
    修复,与两个安全开关无关,故即便两开关都关也要挂上。
    """
    if HookMatcher is None:
        return None
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[pretool_guard_hook])]}
