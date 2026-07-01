"""危险命令拦截 —— PreToolUse hook,拦下真正毁灭性的 Bash 命令。

claude-hermes 默认 bypassPermissions(工具自动执行),方便但有风险。这里加一道
**保守的安全网**:只拦真正会造成灾难的命令(删整个根/家目录、格式化磁盘、覆写裸盘、
fork 炸弹等),宁可漏放也别误伤日常操作——`rm -rf ./build` 这种子目录删除【不拦】。

被拦时返回 permissionDecision=deny,agent 收到拒绝原因,可改用更安全的方式或让用户手动执行。
想关掉:.env 里 DANGER_GUARD=0。
"""
from __future__ import annotations

import re

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
