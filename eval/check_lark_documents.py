"""真实 SDK 业务沙箱调用飞书宿主代理；只读已有文档，不消耗模型额度。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "local-probe-not-a-secret"}):
    from vococo.core import execution
    from vococo.tools.lark_documents import lark_document
    from vococo.tools import danger
from check_business_sandbox import stream_events


def probe_events(document: str, results: list, legacy_first: bool):
    completed = len(results) >= (2 if legacy_first else 1)
    events = stream_events(None if completed else "probe")
    if completed:
        return events
    block = events[1]["content_block"]
    block["id"] = f"tool_probe_{len(results)}"
    if legacy_first and not results:
        block["name"] = "Bash"
        args = {"command": f"lark-cli docs +fetch --doc {document} --detail with-ids --as user"}
    else:
        block["name"] = "mcp__vococo__lark_document"
        args = {"operation": "fetch", "doc": document, "scope": "outline", "max_depth": 1}
    events[2]["delta"]["partial_json"] = json.dumps(args)
    return events


def handler_for(document: str, legacy_first: bool):
    class Handler(BaseHTTPRequestHandler):
        results = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.endswith("count_tokens"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":10}')
                return
            results = [item for msg in body.get("messages", []) if isinstance(msg.get("content"), list)
                       for item in msg["content"] if item.get("type") == "tool_result"]
            Handler.results.extend(results)
            events = probe_events(document, results, legacy_first)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
    return Handler


async def probe(base: Path, document: str, legacy_first: bool = False):
    handler = handler_for(document, legacy_first)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    policy = execution.ExecutionPolicy("0000000000", base, base, (base,))
    options = ClaudeAgentOptions(
        cwd=str(base), model="claude-sonnet-4-5", permission_mode="bypassPermissions",
        allowed_tools=["mcp__vococo__lark_document", "Bash"], tools=["Bash"], max_turns=4,
        hooks=danger.build_hooks(),
        setting_sources=[], strict_mcp_config=True, sandbox=execution.sandbox_settings(policy),
        mcp_servers={"vococo": create_sdk_mcp_server("vococo", tools=[lark_document])},
        env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
             "ANTHROPIC_API_KEY": "local-probe-not-a-secret", "ANTHROPIC_AUTH_TOKEN": "",
             "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    )
    try:
        with patch.object(danger, "_business_policy", return_value=policy):
            async with asyncio.timeout(150):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query("重试上次飞书文档操作")
                    async for _ in client.receive_response():
                        pass
        assert handler.results, "未取得真实工具结果"
        successes = [r for r in handler.results if not r.get("is_error")]
        text = json.dumps(successes, ensure_ascii=False)
        assert 'document_id' in text and 'outline' in text, handler.results
        if legacy_first:
            denied = [r for r in handler.results if r.get("is_error")]
            assert denied and "mcp__vococo__lark_document" in json.dumps(denied), handler.results
            print("PASS：真实 SDK 拦截旧 Bash 飞书命令并提供迁移提示，随后 MCP 宿主读取成功。")
        else:
            assert len(successes) == len(handler.results), handler.results
            print("PASS：真实 SDK 业务沙箱经 MCP 宿主代理读取飞书成功，未放宽沙箱或证书校验。")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="已有文档 URL 或 token，仅只读验证")
    parser.add_argument("--legacy-first", action="store_true", help="先重放旧 Bash 命令，再验证迁移工具调用")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vococo-lark-check-") as directory:
        asyncio.run(probe(Path(directory), args.document, args.legacy_first))
