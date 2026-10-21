"""真实 SDK/CLI → 本地 HTTP 接收端，不调用外部模型。"""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from vococo import providers


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize('summary', ['concise', 'detailed', 'none'])
async def test_real_cli_sends_verbosity_and_effort_to_proxy(tmp_path, summary):
    received = []

    async def messages(request):
        body = await request.json()
        received.append(body)
        response = {"id": "msg_wire_test", "type": "message", "role": "assistant",
                    "model": body["model"], "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1}}
        if not body.get("stream"):
            return web.json_response(response)
        events = [
            ("message_start", {"type": "message_start", "message": {**response, "content": [], "stop_reason": None}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        return web.Response(text="".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events),
                            content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    async with TestServer(app) as server:
        options = ClaudeAgentOptions(
            model="gpt-6.1-sol", cwd=str(tmp_path), system_prompt="Reply ok.",
            tools=[], max_turns=1, setting_sources=[], effort="high",
            env={"ANTHROPIC_BASE_URL": str(server.make_url("/")).rstrip("/"),
                 "ANTHROPIC_API_KEY": "fake-local-only", "CLAUDE_CODE_OAUTH_TOKEN": "",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "CLAUDE_CODE_EXTRA_BODY": providers.extra_body_for_model("gpt-6.1-sol", "high", "low", summary)},
        )
        async with asyncio.timeout(30):
            async with ClaudeSDKClient(options=options) as client:
                await client.query("hi")
                async for _ in client.receive_response():
                    pass
    assert received, "CLI 没有发送模型请求"
    assert received[-1]["text"]["verbosity"] == "low"
    assert received[-1]["output_config"]["effort"] == "high"

    assert received[-1]["reasoning"]["summary"] == (None if summary == "none" else summary)
