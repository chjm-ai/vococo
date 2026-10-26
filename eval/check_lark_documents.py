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
from check_business_sandbox import stream_events


def handler_for(document: str):
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
            events = stream_events(None if results else "probe")
            if not results:
                events[1]["content_block"]["name"] = "mcp__vococo__lark_document"
                events[2]["delta"]["partial_json"] = json.dumps({
                    "operation": "fetch", "doc": document, "scope": "outline", "max_depth": 1})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
    return Handler


async def probe(base: Path, document: str):
    handler = handler_for(document)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    policy = execution.ExecutionPolicy("0000000000", base, base, (base,))
    options = ClaudeAgentOptions(
        cwd=str(base), model="claude-sonnet-4-5", permission_mode="bypassPermissions",
        allowed_tools=["mcp__vococo__lark_document"], tools=[], max_turns=3,
        setting_sources=[], strict_mcp_config=True, sandbox=execution.sandbox_settings(policy),
        mcp_servers={"vococo": create_sdk_mcp_server("vococo", tools=[lark_document])},
        env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
             "ANTHROPIC_API_KEY": "local-probe-not-a-secret", "ANTHROPIC_AUTH_TOKEN": "",
             "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    )
    try:
        async with asyncio.timeout(150):
            async with ClaudeSDKClient(options=options) as client:
                await client.query("用受控工具读取飞书文档目录")
                async for _ in client.receive_response():
                    pass
        assert handler.results and all(not r.get("is_error") for r in handler.results), handler.results
        text = json.dumps(handler.results, ensure_ascii=False)
        assert 'document_id' in text and 'outline' in text, text
        print("PASS：真实 SDK 业务沙箱经 MCP 宿主代理读取飞书成功，未放宽沙箱或证书校验。")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="已有文档 URL 或 token，仅只读验证")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vococo-lark-check-") as directory:
        asyncio.run(probe(Path(directory), args.document))
