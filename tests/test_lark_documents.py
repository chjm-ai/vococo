"""飞书凭据代理边界：只允许固定文档操作，不接受命令或本机文件。"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from vococo.tools import lark_documents as lark


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("args", [
    {"operation": "logout"},
    {"operation": "fetch", "doc": "@/etc/passwd"},
    {"operation": "fetch", "doc": "https://localhost/docx/abc"},
    {"operation": "fetch", "doc": "https://evil.example/docx/abc"},
    {"operation": "fetch", "doc": "abc", "command": "whoami"},
    {"operation": "create", "content": "@/etc/passwd"},
    {"operation": "create", "content": '正文\n<source path="@./pyproject.toml"/>'},
    {"operation": "create", "content": "正文\n![logo](@./docs/images/logo.png)"},
    {"operation": "create", "content": '<img href="http://127.0.0.1/private"/>'},
    {"operation": "update", "doc": "abc", "content": "x", "mode": "delete"},
    {"operation": "fetch", "doc": "abc", "scope": "section"},
])
def test_rejects_commands_files_and_invalid_parameters(args):
    with pytest.raises(ValueError):
        lark.command_for(args)


def test_content_is_stdin_not_cli_file_expansion():
    argv, content = lark.command_for({"operation": "create", "title": "标题", "content": "正文\n$(whoami)"})
    assert argv[:2] == ["docs", "+create"]
    assert argv[argv.index("--content") + 1] == "-"
    assert "$(whoami)" not in argv
    assert content == "正文\n$(whoami)"


@pytest.mark.anyio
async def test_write_denial_never_launches_cli(monkeypatch):
    approve = AsyncMock(return_value=False)
    run = AsyncMock()
    monkeypatch.setattr(lark.danger, "require_approval", approve)
    monkeypatch.setattr(lark, "run_cli", run)
    result = await lark.execute({"operation": "create", "content": "正文"})
    assert result["isError"]
    run.assert_not_awaited()


@pytest.mark.anyio
async def test_write_approval_is_bound_to_target_and_content(monkeypatch):
    approve = AsyncMock(return_value=True)
    run = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(lark.danger, "require_approval", approve)
    monkeypatch.setattr(lark, "run_cli", run)
    await lark.execute({"operation": "update", "doc": "abc", "content": "新增内容", "mode": "append"})
    assert "abc" in approve.call_args.args[1]
    assert "新增内容" in approve.call_args.args[1]
    run.assert_awaited_once()


@pytest.mark.anyio
async def test_real_subprocess_refresh_serialization_and_readback(tmp_path, monkeypatch):
    # 假 CLI 模拟两次调用争用轮换凭证；实际运行子进程而非 mock communicate。
    binary = tmp_path / "lark-cli"
    token = tmp_path / "token"
    binary.write_text("#!/usr/bin/env python3\nimport pathlib,time,json,sys\n"
                      f"p=pathlib.Path({str(token)!r})\n"
                      "n=int(p.read_text()) if p.exists() else 0\n"
                      "time.sleep(.05)\np.write_text(str(n+1))\n"
                      "print(json.dumps({'ok':True,'data':{'generation':n+1,'content':sys.stdin.read()}}))\n")
    binary.chmod(0o700)
    monkeypatch.setattr(lark.shutil, "which", lambda _: str(binary))
    lark._lock = asyncio.Lock()
    results = await asyncio.gather(*(lark.run_cli(["docs", "+fetch"], "正文") for _ in range(3)))
    assert [r["data"]["generation"] for r in results] == [1, 2, 3]
    assert token.read_text() == "3"
    assert all(r["data"]["content"] == "正文" for r in results)


@pytest.mark.anyio
async def test_classifies_tls_without_requesting_relogin(monkeypatch):
    monkeypatch.setattr(lark, "run_cli", AsyncMock(return_value={
        "ok": False, "error": {"type": "network", "subtype": "tls", "message": "OSStatus -26276"}}))
    result = await lark.execute({"operation": "fetch", "doc": "abc"})
    assert result["isError"]
    assert "tls" in result["content"][0]["text"]
    assert "重新登录" not in result["content"][0]["text"]


@pytest.mark.anyio
async def test_real_stderr_error_is_preserved(tmp_path, monkeypatch):
    binary = tmp_path / "lark-cli"
    binary.write_text("#!/usr/bin/env python3\nimport sys\n"
                      "print('{\"ok\":false,\"error\":{\"subtype\":\"tls\"}}',file=sys.stderr)\n"
                      "sys.exit(4)\n")
    binary.chmod(0o700)
    monkeypatch.setattr(lark.shutil, "which", lambda _: str(binary))
    result = await lark.run_cli(["docs", "+fetch"], "")
    assert result == {"ok": False, "error": {"subtype": "tls"}}


@pytest.mark.anyio
async def test_cancel_reaps_wrapper_and_native_child(tmp_path, monkeypatch):
    import subprocess
    import sys

    binary, pidfile = tmp_path / "lark-cli", tmp_path / "child.pid"
    binary.write_text(f"#!{sys.executable}\nimport subprocess,sys,time,pathlib\n"
                      "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
                      f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
                      "time.sleep(30)\n")
    binary.chmod(0o700)
    monkeypatch.setattr(lark.shutil, "which", lambda _: str(binary))
    task = asyncio.create_task(lark.run_cli(["docs", "+fetch"], ""))
    try:
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        status = subprocess.run(["ps", "-p", pidfile.read_text(), "-o", "stat="], capture_output=True, text=True)
        assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_registered_tool_and_business_guidance():
    from vococo.core import execution
    from vococo.tools.builtin import build_mcp_servers
    assert "vococo" in build_mcp_servers()
    assert lark.lark_document.name == "lark_document"
    from pathlib import Path
    policy = execution.ExecutionPolicy("test", Path("/tmp/work"), Path("/tmp/out"), ())
    note = execution.prompt_note(policy)
    assert "官方 lark-cli 的完整原生能力" in note
    assert "按需发现" in note
    assert "必须用 lark_document" not in note
    description = lark.lark_document.description
    assert "不代表飞书完整能力" in description
    assert "尚无宿主入口" not in description
