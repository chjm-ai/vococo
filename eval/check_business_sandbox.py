"""离线 SDK/CLI 沙箱验收：本机假模型，仅操作自建探针目录，不使用真实凭据。"""
from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


def stream_events(command: str | None) -> list[dict]:
    block = {"type": "tool_use", "id": "tool_probe", "name": "Bash", "input": {}} if command else {"type": "text", "text": ""}
    delta = {"type": "input_json_delta", "partial_json": json.dumps({"command": command})} if command else {"type": "text_delta", "text": "验收结束"}
    return [
        {"type": "message_start", "message": {"id": "msg_probe", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if command else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]


def handler_for(command: str):
    class Handler(BaseHTTPRequestHandler):
        results = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.endswith("count_tokens"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 10}')
                return
            results = [item for msg in body.get("messages", []) if isinstance(msg.get("content"), list)
                       for item in msg["content"] if item.get("type") == "tool_result"]
            Handler.results.extend(results)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in stream_events(None if results else command):
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
    return Handler


async def probe(base: Path) -> None:
    work, output, outside = (base / name for name in ("workspace", "iCloud成果", "protected-source"))
    for path in (work, output, outside):
        path.mkdir()
    protected = work / "agent.json"
    protected.write_text("original-policy")
    python = (
        "from pathlib import Path\n"
        f"Path({str(work / 'ok.txt')!r}).write_text('ok')\n"
        f"Path({str(output / 'report.txt')!r}).write_text('report')\n"
        f"for target in {[str(outside / 'must-not-exist.txt'), str(protected)]!r}:\n"
        " try:\n  Path(target).write_text('bad')\n"
        " except PermissionError:\n  print('DENIED', target)\n"
    )
    import shlex
    handler = handler_for("python3 -c " + shlex.quote(python))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    options = ClaudeAgentOptions(
        cwd=str(work), model="claude-sonnet-4-5", permission_mode="bypassPermissions",
        tools=["Bash"], max_turns=3, setting_sources=[], strict_mcp_config=True,
        env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
             "ANTHROPIC_API_KEY": "local-probe-not-a-secret", "ANTHROPIC_AUTH_TOKEN": "",
             "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True,
                 "allowUnsandboxedCommands": False, "excludedCommands": [],
                 "filesystem": {"allowWrite": [str(output)], "denyWrite": [str(protected)]},
                 "network": {"allowedDomains": ["*"]}},
    )
    try:
        async with asyncio.timeout(90):
            async with ClaudeSDKClient(options=options) as client:
                await client.query("运行一次本机文件隔离探针")
                async for _ in client.receive_response():
                    pass
        assert (work / "ok.txt").read_text() == "ok", handler.results
        assert (output / "report.txt").read_text() == "report", handler.results
        assert not (outside / "must-not-exist.txt").exists(), "沙箱未拦住源码目录写入"
        assert protected.read_text() == "original-policy", "业务进程改写了自身执行策略"
        assert handler.results, "未收到真实 Bash 工具结果，不能算验收通过"
        print("PASS：真实 SDK/CLI Bash 可写工作区和指定成果目录，拒绝写外部源码探针。")
        print(json.dumps(handler.results, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix=".vococo-sandbox-check-", dir=Path.home()) as folder:
        asyncio.run(probe(Path(folder)))
