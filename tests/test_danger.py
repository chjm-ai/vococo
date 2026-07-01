"""危险命令拦截:灾难命令拦下、日常命令放行(不误伤)、hook 输出正确。"""
from __future__ import annotations

import anyio
import pytest

from claude_hermes.tools.danger import is_dangerous, pretool_danger_hook

DANGEROUS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "sudo rm -rf /",
    "rm -fr $HOME",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/disk2",
    "echo x > /dev/sda",
    "chmod -R 777 /",
    ":(){ :|:& };:",
]

SAFE = [
    "rm -rf ./build",
    "rm -rf node_modules",
    "rm -rf /tmp/foo",
    "rm -rf ~/Downloads/junk",
    "rm file.txt",
    "ls -la /",
    "git push origin main",
    "python -m pytest",
    "chmod -R 777 ./cache",
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_dangerous_flagged(cmd):
    assert is_dangerous(cmd) is not None, f"应拦截: {cmd}"


@pytest.mark.parametrize("cmd", SAFE)
def test_safe_allowed(cmd):
    assert is_dangerous(cmd) is None, f"不该误伤: {cmd}"


def test_hook_denies_dangerous_bash():
    out = anyio.run(
        lambda: pretool_danger_hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, None, {}
        )
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "拦截" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_safe_bash():
    out = anyio.run(
        lambda: pretool_danger_hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, None, {}
        )
    )
    assert out == {}


def test_hook_ignores_non_bash():
    out = anyio.run(
        lambda: pretool_danger_hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, None, {}
        )
    )
    assert out == {}
