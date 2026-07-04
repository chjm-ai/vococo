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

# 其余灾难级模式(literal)。注:黑名单是「减速带」不是「安全边界」——枚举永远不完整,
# 真正的防线是边界 fail-closed + 缩小注入爆炸半径(见 安全策略优化方案.md)。这里只补
# 「一眼灾难、日常绝不会误伤」的少数模式,提高门槛而已。
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(disk|sd|nvme|rdisk)"), "dd 写裸磁盘"),
    (re.compile(r">\s*/dev/(sd|nvme|disk|rdisk)"), "覆写裸磁盘"),
    (re.compile(r":\s*\(\s*\)\s*\{.*[|&].*\}\s*;"), "fork 炸弹"),
    (re.compile(r"\bchmod\s+-R\s+0*777\s+/(\s|$)"), "chmod -R 777 根目录"),
    # 解释器内置删根/家目录:python -c "import shutil; shutil.rmtree('/')" 之类
    (re.compile(r"\b(shutil\.rmtree|os\.removedirs)\s*\(\s*['\"](/|~|\$HOME)['\"/]"),
     "解释器删根/家目录"),
]

# find 删整树:必须同时 ①是 find ②带 -delete ③目标是灾难级(根/家目录/根通配),复用
# 已验证的 _CATASTROPHIC_TARGET,避免 "find /tmp -delete"、"find ./x -delete" 被误伤。
_FIND_DELETE = re.compile(r"\bfind\b")
_HAS_DELETE = re.compile(r"\s-delete\b")


def _find_is_catastrophic(cmd: str) -> bool:
    return bool(
        _FIND_DELETE.search(cmd)
        and _HAS_DELETE.search(cmd)
        and _CATASTROPHIC_TARGET.search(cmd)
    )


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
    if _find_is_catastrophic(c):
        return "find 删整树(根/家目录)"
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
# 第三个元素 restrict_noninteractive:非交互通道(cron/eval,无人可点审批)是否直接拒绝。
# 只对「对外/供应链/改环境」这三类置 True(push/装包/curl|sh)——它们在自动化里被注入
# 后果最重;rm -rf / reset 是本地操作,自动化里放行(避免卡住日常定时任务)。
_ESCALATE_BASH: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"\bgit\s+push\b"), "git push(推送到远端,对外)", True),
    (re.compile(r"\bgit\s+reset\b[^\n]*--hard\b"), "git reset --hard(丢弃改动,不可逆)", False),
    (re.compile(r"\brm\s+-\S*[rR]"), "rm -rf(递归删除)", False),
    (
        re.compile(
            r"\b(pip3?|pipx|uv|npm|pnpm|yarn|brew|apt|apt-get|gem|cargo|go)\s+"
            r"(install|add|get|i)\b"
        ),
        "包安装(改动环境)",
        True,
    ),
    (re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
     "curl|sh(下载执行,供应链风险)", True),
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


def classify(
    tool_name: str, tool_input: dict, cwd: str | None = None
) -> tuple[str, str, bool]:
    """把一次工具调用分成 allow / escalate / block,附(原因, 非交互是否拒绝)。

    - block:灾难级(删根/格式化…),直接拦。
    - escalate:5 类危险操作,请用户批准。
    - allow:其余,放行。

    第三个返回值 restrict_noninteractive:该 escalate 操作在无交互通道(cron/eval)时
    是否应默认拒绝(fail-closed)。allow/block 场景恒为 False(无意义)。
    """
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = ti.get("command", "") or ""
        why = is_dangerous(cmd)
        if why:
            return ("block", why, False)
        for rx, reason, restrict in _ESCALATE_BASH:
            if rx.search(cmd):
                return ("escalate", reason, restrict)
        return ("allow", "", False)
    if tool_name in _WRITE_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if _outside_cwd(path, cwd):
            # 写工作目录外:自动化通道也拒绝(可能被注入用来落地后门/改配置)
            return ("escalate", f"写工作目录外的文件({path})", True)
    return ("allow", "", False)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ── 强制前台执行:把 run_in_background 改写成 false ─────────────────────────────
# 本 harness 每轮用一个 ClaudeSDKClient,收到【本轮 ResultMessage】就退出 receive_response
# 并关闭子进程。而 run_in_background 的子代理/命令是「立即返回、真正干活排到 ResultMessage
# 之后以 task_* 系统消息陆续上报」—— 届时子进程已被关掉,任务被腰斩:模型嘴上说「已在后台
# 发起」,实际一步没跑,也没有任何结果/显示。故在此把后台标志改写成前台,让它们【在本轮内
# 同步跑完】,既真执行、又能靠既有的子代理卡片实时显示。
# 子代理工具新版叫 Agent、老版叫 Task,两者都收;Bash 也有 run_in_background。
_BACKGROUNDABLE = {"Agent", "Task", "Bash"}


def _force_foreground(tool_name: str, tool_input: dict) -> dict | None:
    """请求了后台执行 → 返回改写成前台的入参副本;否则 None(不改动)。"""
    if tool_name not in _BACKGROUNDABLE:
        return None
    ti = tool_input or {}
    if not ti.get("run_in_background"):
        return None
    patched = dict(ti)
    patched["run_in_background"] = False
    return patched


def _allow_with_input(updated_input: dict) -> dict:
    """放行并改写入参(PreToolUse hook 的 updatedInput 机制)。"""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }


def _describe(tool_name: str, tool_input: dict) -> str:
    """给审批弹窗一行「具体要干什么」。"""
    ti = tool_input or {}
    if tool_name == "Bash":
        return f"`{(ti.get('command') or '').strip()[:200]}`"
    if tool_name in _WRITE_TOOLS:
        return f"→ {ti.get('file_path') or ti.get('notebook_path') or '?'}"
    return tool_name


def _is_group_session(session_key: str) -> bool:
    """群聊会话(TG 群 chat_id 为负 → key 形如 tg:-123)。群里危险操作不许自批。"""
    return bool(session_key) and session_key.startswith("tg:")


async def _approve(reason: str, detail: str, restrict_noninteractive: bool) -> bool:
    """审批底座:有交互通道 → 弹「允许一次 / 拒绝」按钮并阻塞等。

    - 群聊会话:一律拒绝(批准权不能落在群成员/被拉进群的陌生人手里,见审计 #4)。
    - 无交互通道(cron/eval):按 restrict_noninteractive 决定——「对外/装包/持久化」类
      默认拒绝(fail-closed,"无人可问"≠"同意"),本地操作放行。
    - 复用 ask_user 同款 clarify 机制:回复经网关「拿锁前 resolve」解除,不会死锁。
      超时 / 发送失败 → 视为拒绝(危险操作宁可不做)。
    """
    from ..gateway import clarify
    from ..gateway.core import Choice

    ctx = clarify.current()
    if ctx is None:
        return not restrict_noninteractive
    if _is_group_session(ctx.session_key):
        return False
    p = clarify.register(ctx.session_key, ["允许一次", "拒绝"])
    try:
        opts = [
            (f"/clarify {p.clarify_id} 0", "✅ 允许一次"),
            (f"/clarify {p.clarify_id} 1", "🛑 拒绝"),
        ]
        prompt = f"⚠️ 需要批准:{reason}\n{detail}"
        await ctx.adapter.present_choice(ctx.chat_id, Choice(prompt=prompt, options=opts))
    except Exception:
        clarify.resolve(p.clarify_id, "拒绝")
        return False
    answer = await clarify.wait(p.clarify_id, config.CLARIFY_TIMEOUT)
    return answer == "允许一次"


async def _ask_approval(
    tool_name: str, reason: str, tool_input: dict, restrict_noninteractive: bool = False
) -> bool:
    """PreToolUse 审批闸调用的入口:把工具入参渲染成一行说明后走 _approve。"""
    return await _approve(reason, _describe(tool_name, tool_input), restrict_noninteractive)


async def require_approval(
    reason: str, detail: str, *, restrict_noninteractive: bool = True
) -> bool:
    """供 MCP 工具(如 cron 启用/删除)复用的审批。默认非交互通道拒绝(持久化类操作
    不该在 cron 上下文里被 agent 静默改动)。"""
    return await _approve(reason, detail, restrict_noninteractive)


async def pretool_guard_hook(input_data, tool_use_id, context):
    """PreToolUse hook:灾难级 → 拦;危险级 → 请批准;其余放行。

    这是接进 SDK 的那个 hook(build_hooks)。旧的 pretool_danger_hook 只做灾难拦截,
    保留供既有测试;新逻辑在此,由 DANGER_GUARD / APPROVAL_GATE 两开关分别控制。
    """
    tool_name = input_data.get("tool_name", "") or ""
    tool_input = input_data.get("tool_input") or {}
    try:
        verdict, reason, restrict = classify(tool_name, tool_input, cwd=current_cwd())
    except Exception:
        # classify 是纯正则,几乎不会异常;真异常时无从判定 → 放行,不阻断正常流程
        return {}
    if verdict == "block" and config.DANGER_GUARD:
        return _deny(
            f"⛔ 危险命令被 Hermes 拦截({reason})。如确需执行,请你手动在终端运行。"
        )
    if verdict == "escalate" and config.APPROVAL_GATE:
        try:
            approved = await _ask_approval(tool_name, reason, tool_input, restrict)
        except Exception:
            # 审批过程本身异常(而非模型正常操作)→ fail-closed:疑似危险操作宁可拒绝
            return _deny(f"🛑 审批过程异常,已保守拒绝此操作({reason})。请手动执行或重试。")
        if not approved:
            return _deny(
                f"🛑 你未批准此操作({reason})。已跳过;如需执行请手动运行或改用更安全方式。"
            )
    # 放行:若请求了后台执行,改写成前台(见 _force_foreground 说明),否则默认放行
    try:
        patched = _force_foreground(tool_name, tool_input)
        if patched is not None:
            return _allow_with_input(patched)
    except Exception:
        pass  # 前台改写失败不阻断:回退成默认放行
    return {}


def build_hooks() -> dict | None:
    """返回挂给 ClaudeAgentOptions.hooks 的结构;SDK 不支持则 None。

    始终挂 PreToolUse:除危险拦截/审批闸(各由 DANGER_GUARD/APPROVAL_GATE 开关控制)外,
    还负责把 run_in_background 改写成前台执行——这是纠正「后台任务被腰斩」的正确性修复,
    与两个安全开关无关,故即便两开关都关也要挂上。
    """
    if HookMatcher is None:
        return None
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[pretool_guard_hook])]}
