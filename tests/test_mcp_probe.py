"""真实 HTTP + 官方 MCP 客户端验证探针，不启动 Agent、不使用生产数据。"""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from vococo.gateway.mcp_probe import create_server, public_origin


@asynccontextmanager
async def streamablehttp_client(url):
    async with httpx2.AsyncClient(trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            yield streams


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = tmp_path_factory.mktemp("mcp-probe") / "probe.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "vococo.gateway.mcp_probe", "--port", str(port),
             "--public-origin", "https://probe.example.com"],
            cwd=Path(__file__).resolve().parents[1], stdout=output, stderr=output,
        )
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(log.read_text())
                try:
                    httpx.get(url.replace("/mcp", "/settings"), timeout=0.2, trust_env=False)
                    break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                pytest.fail("MCP 测试服务启动超时")
            yield url, log
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_real_client_discovers_only_probe_and_receipt_matches_log(probe):
    async def exercise():
        async with streamablehttp_client(probe[0]) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.server_info.name == "vococo-probe"
                tools = (await client.list_tools()).tools
                assert {tool.name for tool in tools} == {"vococo_ping", "vococo_delay", "vococo_long_reply"}
                assert all(tool.annotations.read_only_hint for tool in tools)
                first = await client.call_tool("vococo_ping", {"challenge": "voice_test_01"})
                second = await client.call_tool("vococo_ping", {"challenge": "voice_test_01"})
                assert not first.is_error and not second.is_error
                result = first.structured_content
                assert result["challenge"] == "voice_test_01"
                assert result["capability"] == "connectivity_only"
                assert result["receipt"] != second.structured_content["receipt"]
                assert len(result["receipt"]) == 32
                return result["receipt"]
    receipt = asyncio.run(exercise())
    assert receipt in probe[1].read_text()
    assert "voice_test_01" not in probe[1].read_text()


def test_invalid_arguments_and_unknown_tools_cannot_execute(probe):
    async def exercise():
        async with streamablehttp_client(probe[0]) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                for arguments in [{}, {"challenge": ""}, {"challenge": "a" * 65},
                                  {"challenge": "private text"}, {"challenge": "\n"}]:
                    result = await client.call_tool("vococo_ping", arguments)
                    assert result.is_error
                unknown = await client.call_tool("create_task", {"prompt": "do nothing"})
                assert unknown.is_error
                boundary = await client.call_tool("vococo_ping", {"challenge": "a" * 64})
                assert not boundary.is_error
    asyncio.run(exercise())


@pytest.mark.parametrize("characters", [256, 4096, 32768])
def test_long_reply_has_exact_unicode_length_hash_and_end_marker(probe, characters):
    async def exercise():
        async with streamablehttp_client(probe[0]) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool("vococo_long_reply", {"characters": characters})
                assert not response.is_error
                result = response.structured_content
                text = result["text"]
                assert len(text) == result["characters"] == characters
                assert len(text.encode("utf-8")) == result["utf8_bytes"] > characters
                assert hashlib.sha256(text.encode("utf-8")).hexdigest() == result["sha256"]
                assert text.startswith("BEGIN_" + result["receipt"])
                assert text.endswith("END_" + result["receipt"])
                assert result["server_elapsed_ms"] >= 0
    asyncio.run(exercise())


def test_delay_does_not_block_ping_and_has_bounded_concurrency(probe):
    async def exercise():
        async with streamablehttp_client(probe[0]) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                pending = [asyncio.create_task(client.call_tool("vococo_delay", {"delay_ms": 1000}))
                           for _ in range(2)]
                await asyncio.sleep(0.15)
                ping = await client.call_tool("vococo_ping", {"challenge": "parallel_test"})
                assert not ping.is_error and not any(task.done() for task in pending)
                busy = await client.call_tool("vococo_delay", {"delay_ms": 0})
                assert busy.is_error
                for response in await asyncio.gather(*pending):
                    assert not response.is_error
                    assert response.structured_content["server_elapsed_ms"] >= 990
                zero = await client.call_tool("vococo_delay", {"delay_ms": 0})
                assert not zero.is_error
    asyncio.run(exercise())


def test_complex_probe_rejects_out_of_bounds_or_non_integer_inputs(probe):
    async def exercise():
        async with streamablehttp_client(probe[0]) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                for name, field, values in [
                    ("vococo_delay", "delay_ms", [-1, 5001, True, 1.5, "1000"]),
                    ("vococo_long_reply", "characters", [255, 32769, True, 4096.5, "4096"]),
                ]:
                    for value in values:
                        response = await client.call_tool(name, {field: value})
                        assert response.is_error, (name, value)
    asyncio.run(exercise())


def test_host_origin_and_web_routes_are_restricted(probe):
    url = probe[0]
    headers = {"Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "probe-test", "version": "1"},
    }}
    assert httpx.post(url, trust_env=False, json=body, headers={**headers, "Host": "evil.example"}).status_code == 421
    assert httpx.post(url, trust_env=False, json=body, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    public = httpx.post(url, trust_env=False, json=body, headers={**headers, "Host": "probe.example.com"})
    assert public.status_code == 200
    assert "result" in public.json()
    assert httpx.get(url.replace("/mcp", "/settings"), trust_env=False).status_code == 404
    assert httpx.get(url.replace("/mcp", "/tasks"), trust_env=False).status_code == 404
    malformed = httpx.post(url, trust_env=False, content="{", headers={**headers, "Content-Type": "application/json"})
    assert malformed.status_code == 400


@pytest.mark.parametrize("value", ["http://example.com", "https://user:pass@example.com",
                                     "https://example.com/mcp", "https://example.com?token=x",
                                     "https://example.com#x", "https://example.com:bad"])
def test_public_origin_rejects_unsafe_or_ambiguous_addresses(value):
    with pytest.raises(ValueError):
        public_origin(value)


def test_configuration_stays_loopback_only():
    server = create_server(8852, "https://PROBE.example.com/")
    assert server._vococo_port == 8852
    assert "probe.example.com" in server._vococo_security.allowed_hosts
    assert server._vococo_security.enable_dns_rebinding_protection
    with pytest.raises(ValueError):
        create_server(0)


def test_measurement_script_runs_full_bounded_suite(probe):
    response = subprocess.run(
        [sys.executable, "scripts/check_mcp_probe.py", probe[0], "--samples", "1"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
    )
    assert response.returncode == 0, response.stderr
    report = json.loads(response.stdout)
    assert len(report["ping_samples"]) == 1
    assert len(report["long_replies"]) == 3
    assert all(row["integrity_ok"] for row in report["long_replies"])
    assert report["delay"]["server_elapsed_ms"] >= 1990
    assert report["nonblocking"]["delay_still_pending_when_ping_returned"]
    assert report["delay"]["receipt"] in probe[1].read_text()
