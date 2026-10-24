"""真实 SDK/CLI 网络回归：本机假 HTTP 代理、SSH 握手，不访问外部服务或 NAS。"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import socketserver
import sys
import tempfile
import threading
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from check_business_sandbox import handler_for

# 共享 venv 的 editable 安装可能指向主仓库，验收必须加载本次 worktree。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "local-probe-not-a-secret"}):
    from vococo.core import execution


class Proxy(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"probe": "ok"}')


class SSH(socketserver.BaseRequestHandler):
    handshake = threading.Event()

    def handle(self):
        self.request.settimeout(5)
        self.request.sendall(b"SSH-2.0-vococo-offline-probe\r\n")
        if self.request.recv(1024).startswith(b"SSH-2.0-"):
            self.handshake.set()


def serve(stack, server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.server_close)
    stack.callback(server.shutdown)
    return server.server_address[1]


def command_for(base, proxy_port, ssh_port):
    code = (
        "import json,urllib.request,subprocess\nfrom pathlib import Path\n"
        f"op=urllib.request.build_opener(urllib.request.ProxyHandler({{'http':'http://127.0.0.1:{proxy_port}'}}))\n"
        "assert json.load(op.open('http://sandbox-probe.invalid/test',timeout=5))['probe']=='ok'\n"
        "print('HTTP_PROXY_OK')\n"
        "r=subprocess.run(['ssh','-F','/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=5',"
        f"'-p','{ssh_port}','127.0.0.1','true'],capture_output=True,text=True,timeout=10)\n"
        "assert 'Operation not permitted' not in r.stderr, r.stderr\n"
        f"Path({str(base / 'output/report.txt')!r}).write_text('ok')\n"
        f"for p in {[str(base / 'source/blocked.txt'), str(base / 'workspace/agent.json')]!r}:\n"
        " try:\n  Path(p).write_text('bad')\n"
        " except PermissionError:\n  print('WRITE_DENIED')\n"
        " else:\n  raise AssertionError('文件保护失效')\n"
        "print('FILE_BOUNDARY_OK')\n"
    )
    return "python3 -c " + shlex.quote(code)


async def probe(base):
    for name in ("workspace", "output", "source"):
        (base / name).mkdir()
    protected = base / "workspace/agent.json"
    protected.write_text("original")
    policy = execution.ExecutionPolicy("0000000000", base / "workspace", base / "output",
                                       (base / "workspace", base / "output"))
    with ExitStack() as stack:
        proxy_port = serve(stack, ThreadingHTTPServer(("127.0.0.1", 0), Proxy))
        ssh_port = serve(stack, socketserver.ThreadingTCPServer(("127.0.0.1", 0), SSH))
        handler = handler_for(command_for(base, proxy_port, ssh_port))
        model_port = serve(stack, ThreadingHTTPServer(("127.0.0.1", 0), handler))
        # 强制复现原脚本的显式本机代理，不依赖当前机器的代理配置。
        stack.enter_context(patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy_port}"}))
        sandbox = execution.sandbox_settings(policy)
        sandbox["filesystem"]["denyWrite"].append(str(protected))
        options = ClaudeAgentOptions(
            cwd=str(policy.workspace), model="claude-sonnet-4-5", permission_mode="bypassPermissions",
            tools=["Bash"], max_turns=3, setting_sources=[], strict_mcp_config=True,
            sandbox=sandbox,
            env={**execution.shell_env(), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model_port}",
                 "ANTHROPIC_API_KEY": "local-probe-not-a-secret", "ANTHROPIC_AUTH_TOKEN": "",
                 "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        async with asyncio.timeout(75):
            async with ClaudeSDKClient(options=options) as client:
                await client.query("运行本机网络和文件边界探针")
                async for _ in client.receive_response():
                    pass
        results = json.dumps(handler.results, ensure_ascii=False)
        assert "HTTP_PROXY_OK" in results, results
        assert SSH.handshake.is_set(), results
        assert "FILE_BOUNDARY_OK" in results, results
        assert not (base / "source/blocked.txt").exists()
        assert protected.read_text() == "original"
        assert (base / "output/report.txt").read_text() == "ok"
        print("PASS：真实沙箱中显式 HTTP 代理成功、普通 subprocess SSH 完成握手；成果可写，源码与执行配置不可写。")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix=".vococo-network-check-", dir=Path.home()) as folder:
        asyncio.run(probe(Path(folder)))
