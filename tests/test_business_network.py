"""业务沙箱兼容已有本机 HTTP 代理及脚本中的普通 SSH 调用。"""
from pathlib import Path
import os
import runpy
import shlex
from unittest.mock import patch

import pytest

from claude_agent_sdk import ClaudeAgentOptions
from vococo.core import execution, subagent
from vococo.tools import danger


@pytest.mark.parametrize("proxy,port", [
    ("http://127.0.0.1:7890", 7890),
    ("http://localhost:8888", 8888),
    ("http://proxy.example:7890", None),
    ("socks5://127.0.0.1:7890", None),
    ("http://user:password@localhost:7890", None),
    ("http://localhost:bad", None),
    ("http://localhost:0", None),
    ("http://[invalid", None),
    ("", None),
])
def test_network_reuses_only_plain_local_http_proxy(monkeypatch, proxy, port):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy)
    network = execution.network_settings()
    assert network["allowedDomains"] == ["*"]
    assert network.get("httpProxyPort") == port
    assert "allowLocalBinding" not in network
    assert "socksProxyPort" not in network  # 保留 SDK 自带 SOCKS 代理，不假定 HTTP 端口支持 SOCKS。


def test_shell_env_puts_ssh_adapter_before_original_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = execution.shell_env()
    first, rest = env["PATH"].split(os.pathsep, 1)
    assert rest == "/usr/bin:/bin"
    assert (Path(first) / "ssh").is_file()
    assert os.access(Path(first) / "ssh", os.X_OK)


@pytest.mark.parametrize("command", [
    "ssh -o ControlMaster=no -o ControlPath=none -o "
    "ProxyCommand='nc -X 5 -x localhost:54321 %h %p'",
    "",
])
def test_ordinary_ssh_inherits_sdk_proxy_without_shell_evaluation(monkeypatch, command):
    monkeypatch.setenv("GIT_SSH_COMMAND", command)
    monkeypatch.setattr("sys.argv", ["ssh", "-p", "16808", "nas", "echo ok"])
    script = Path(execution.__file__).parent / "sandbox_bin" / "ssh"
    with patch("os.execv") as execute:
        runpy.run_path(str(script), run_name="__main__")
    expected = shlex.split(command)[1:] if command else []
    execute.assert_called_once_with("/usr/bin/ssh", ["/usr/bin/ssh", *expected,
                                                    "-p", "16808", "nas", "echo ok"])


@pytest.mark.parametrize("selection", [None, "claude-sonnet-5-5"])
def test_child_model_switch_keeps_network_adapter(monkeypatch, selection):
    monkeypatch.setattr(subagent.providers, "resolve", lambda *a: ("claude-sonnet-5-5", {}))
    monkeypatch.setattr(subagent.settings_store, "get_web_effort", lambda *a: "")
    monkeypatch.setattr(subagent.settings_store, "get_web_verbosity", lambda *a: "")
    monkeypatch.setattr(subagent.settings_store, "get_web_summary", lambda *a: "")
    parent = ClaudeAgentOptions(env={**execution.shell_env(), "CLAUDE_CODE_EXTRA_BODY": "old-provider"},
                               sandbox={"enabled": True}, hooks=danger.build_hooks())
    child = subagent._child_options(parent, selection)
    assert child.env["PATH"] == parent.env["PATH"]
    if selection:
        assert child.env["CLAUDE_CODE_EXTRA_BODY"] != "old-provider"


def test_ssh_adapter_rejects_non_ssh_commands(monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "sh -c 'echo must-not-execute'")
    script = Path(execution.__file__).parent / "sandbox_bin" / "ssh"
    with patch("os.execv") as execute, pytest.raises(SystemExit, match="只支持系统 ssh"):
        runpy.run_path(str(script), run_name="__main__")
    execute.assert_not_called()
