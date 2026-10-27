"""显式指定已核验的官方二进制后，在本机隔离验收 OAuth；默认不下载或执行。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import secrets
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from vococo.gateway import brain_mcp

pytestmark = pytest.mark.anyio
BINARY_SHA256 = "8f78bb80278b331c7c923c966e9d2faceedeffb6231b1e66dbc45fffd1fcb29c"
ORIGIN = "https://mcp.chjm.cc"
PASSWORD = "public-test-only-password-32-chars"
UPSTREAM = "test-only-oauth-upstream-" + "u" * 40
BRIDGE = "test-only-oauth-bridge-" + "b" * 40


@pytest.fixture
def anyio_backend():
    return "asyncio"


def binary_path():
    value = os.environ.get("VOCOCO_MCP_AUTH_TEST_BINARY")
    if not value:
        pytest.skip("未显式指定已核验的 OAuth 二进制；不下载或启动")
    path = Path(value)
    assert path.is_file() and not path.is_symlink()
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == BINARY_SHA256
    return path


def password_hash():
    path = Path(__file__).parents[1] / "scripts/setup_mcp_auth.py"
    spec = importlib.util.spec_from_file_location("mcp_oauth_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.htpasswd_hash(PASSWORD)


class LocalTLS(httpx.AsyncBaseTransport):
    """仅测试：把固定 HTTPS origin 映射到本机明文端口，保留真实 Host/安全 cookie 语义。"""

    def __init__(self, port):
        self.port = port
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        assert str(request.url).startswith(ORIGIN + "/")
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        local = httpx.Request(request.method, url, headers=request.headers,
                             stream=request.stream, extensions=request.extensions)
        return await self.transport.handle_async_request(local)

    async def aclose(self):
        await self.transport.aclose()


async def wait_ready(client):
    for _ in range(100):
        try:
            response = await client.get(ORIGIN + "/.well-known/oauth-authorization-server")
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        await asyncio.sleep(.1)
    pytest.fail("隔离 OAuth 代理未就绪；日志未输出，避免泄漏凭据")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_proxy(binary, port, frontend_port, directory):
    env = {"PATH": os.defpath, "PASSWORD_HASH": password_hash(), "PROXY_BEARER_TOKEN": UPSTREAM,
           "PROXY_FORWARD_AUTHORIZATION": "false", "REPOSITORY_BACKEND": "local"}
    return await asyncio.create_subprocess_exec(
        str(binary), "--external-url", ORIGIN, "--no-auto-tls", "--listen", f"127.0.0.1:{port}",
        "--data-path", str(directory), "--", f"http://127.0.0.1:{frontend_port}", env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


async def stop_owned(process):
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def authorize(client, metadata):
    registration = await client.post(metadata["registration_endpoint"], json={
        "client_name": "local-isolated-test", "redirect_uris": ["http://127.0.0.1:9899/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "offline_access",
    })
    assert registration.status_code in (200, 201), registration.status_code
    client_id = registration.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = await client.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id, "redirect_uri": "http://127.0.0.1:9899/callback",
        "scope": "offline_access", "state": "isolated-random-state-12345678", "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": ORIGIN + "/mcp",
    })
    assert response.status_code in (302, 303)
    if "/.auth/login" not in response.headers["location"]:
        response = await client.get(ORIGIN + response.headers["location"])
    assert "/.auth/login" in response.headers["location"]
    assert (await client.post(ORIGIN + "/.auth/login", data={"password": "wrong"})).status_code == 400
    code = await complete_login(client)
    return client_id, verifier, code


async def complete_login(client):
    response = await client.post(ORIGIN + "/.auth/login", data={"password": PASSWORD})
    for _ in range(5):
        if response.status_code == 200:
            assert response.url.path.startswith("/.idp/auth/")
            assert '<form method="post">' in response.text
            response = await client.post(response.url)
        assert response.status_code in (302, 303)
        location = response.headers["location"]
        if location.startswith("http://127.0.0.1:9899/callback"):
            code = parse_qs(urlsplit(location).query)
            assert code["state"] == ["isolated-random-state-12345678"]
            return code["code"][0]
        response = await client.get(ORIGIN + location if location.startswith("/") else location)
    pytest.fail("隔离授权未到回调")


async def check_mcp(client, token):
    client.headers["Authorization"] = "Bearer " + token
    async with streamable_http_client(ORIGIN + "/mcp", http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 5
            result = await session.call_tool("vococo_list_sessions", {})
            assert not result.isError and result.structuredContent["sessions"] == []
    del client.headers["Authorization"]


async def test_real_proxy_pkce_login_refresh_mcp_and_persistence(tmp_path, monkeypatch):
    binary = binary_path()
    monkeypatch.setenv("VOCOCO_MCP_UPSTREAM_TOKEN", UPSTREAM)
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", BRIDGE)
    seen = []

    async def backend(request):
        assert request.headers["Authorization"] == "Bearer " + BRIDGE
        seen.append(await request.json())
        return web.json_response({"sessions": [], "next_offset": None})

    app = web.Application()
    app.router.add_post("/internal/brain/list_sessions", backend)
    async with TestServer(app) as bridge:
        await run_proxy_test(binary, bridge.port, tmp_path, seen)


async def run_proxy_test(binary, bridge_port, directory, seen):
    frontend_port, proxy_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(brain_mcp.create_app(brain_mcp.create_server(frontend_port, bridge_port)),
                                          host="127.0.0.1", port=frontend_port, access_log=False, log_level="error"))
    frontend = asyncio.create_task(server.serve())
    proxy = await start_proxy(binary, proxy_port, frontend_port, directory)
    try:
        async with httpx.AsyncClient(transport=LocalTLS(proxy_port), follow_redirects=False) as client:
            metadata = await wait_ready(client)
            assert metadata["issuer"].rstrip("/") == ORIGIN
            assert (await client.post(ORIGIN + "/mcp", json={})).status_code == 401
            client_id, verifier, code = await authorize(client, metadata)
            tokens = await exchange(client, metadata, client_id, verifier, code)
            await check_mcp(client, tokens["access_token"])
            await stop_owned(proxy)
            refreshed = await check_restart(binary, proxy_port, frontend_port, directory, client, metadata, tokens)
            assert seen == [{"offset": 0, "limit": 20}] * 4
            await check_reset(binary, proxy_port, frontend_port, directory, client, metadata, tokens, refreshed)
    finally:
        await stop_owned(proxy)
        server.should_exit = True
        await asyncio.wait_for(frontend, 10)


async def check_restart(binary, port, frontend, directory, client, metadata, tokens):
    proxy = await start_proxy(binary, port, frontend, directory)
    try:
        await wait_ready(client)
        await check_mcp(client, tokens["access_token"])
        response = await client.post(metadata["token_endpoint"], data={"grant_type": "refresh_token",
            "client_id": tokens["_client_id"], "refresh_token": tokens["refresh_token"]})
        assert response.status_code == 200, response.status_code
        refreshed = response.json()
        await check_mcp(client, refreshed["access_token"])
        # 登出只结束浏览器会话，不把它错误当成 token 撤销。
        await client.get(ORIGIN + "/.auth/logout")
        await check_mcp(client, refreshed["access_token"])
        return refreshed
    finally:
        await stop_owned(proxy)


async def check_reset(binary, port, frontend, directory, client, metadata, tokens, refreshed):
    fresh = directory / "reset-all"
    fresh.mkdir(mode=0o700)
    proxy = await start_proxy(binary, port, frontend, fresh)
    try:
        await wait_ready(client)
        denied = await client.post(ORIGIN + "/mcp", json={},
            headers={"Authorization": "Bearer " + refreshed["access_token"]})
        assert denied.status_code == 401
        old_refresh = await client.post(metadata["token_endpoint"], data={"grant_type": "refresh_token",
            "client_id": tokens["_client_id"], "refresh_token": refreshed["refresh_token"]})
        assert old_refresh.status_code in (400, 401)
    finally:
        await stop_owned(proxy)


async def exchange(client, metadata, client_id, verifier, code):
    data = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": "http://127.0.0.1:9899/callback", "code_verifier": verifier}
    wrong = await client.post(metadata["token_endpoint"], data={**data, "code_verifier": "wrong" * 10})
    assert wrong.status_code == 400
    # 错误 verifier 可能销毁授权码；重新走一轮，不能假定可重用。
    client_id, verifier, code = await authorize_fresh(client, metadata)
    data.update(client_id=client_id, code=code, code_verifier=verifier)
    response = await client.post(metadata["token_endpoint"], data=data)
    assert response.status_code == 200, response.status_code
    tokens = response.json()
    tokens["_client_id"] = client_id
    return tokens


async def authorize_fresh(client, metadata):
    client.cookies.clear()
    return await authorize(client, metadata)
