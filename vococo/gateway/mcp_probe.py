"""无执行权限的独立 MCP 探针，仅用于验证 ChatGPT 文本/实时语音工具调用。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

_LOG = logging.getLogger("vococo.mcp_probe")
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False,
)


def _receipt(tool: str, **metrics) -> dict:
    result = {"service": "vococo-probe", "receipt": uuid.uuid4().hex,
              "server_time": datetime.now(timezone.utc).isoformat(), **metrics}
    # 不记录客户端挑战词或长正文，只留下可核对的服务端回执与指标。
    _LOG.info("probe_call %s", json.dumps({"tool": tool, **result}))
    return result


def public_origin(value: str) -> str:
    """公网地址只用于收窄 Host/Origin，不用于网络请求。"""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("公网地址必须是纯 HTTPS 域名，例如 https://probe.example.com")
    # 提前拒绝无效端口，避免服务启动后才报错。
    parsed.port
    if not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", parsed.netloc):
        raise ValueError("公网地址必须使用普通域名")
    return f"https://{parsed.netloc.lower()}"


def create_server(port: int = 8852, origin: str | None = None) -> FastMCP:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    origins = [f"http://{host}" for host in hosts]
    if origin:
        canonical = public_origin(origin)
        hosts.append(urlsplit(canonical).netloc)
        origins.append(canonical)
    server = FastMCP(
        "vococo-probe", host="127.0.0.1", port=port,
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts, allowed_origins=origins,
        ),
    )
    _register_probe(server)
    _register_delay(server)
    _register_long_reply(server)
    return server


def _register_probe(server: FastMCP) -> None:
    @server.tool(
        description="测试与 vococo 的真实连接。只返回挑战词、随机回执和 UTC 时间，不执行任务。",
        annotations=_READ_ONLY,
    )
    def vococo_ping(
        challenge: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    ) -> dict[str, str]:
        return {**_receipt("vococo_ping"), "challenge": challenge,
                "capability": "connectivity_only"}


def _register_delay(server: FastMCP) -> None:
    slots = asyncio.Semaphore(2)

    @server.tool(description="只读等待测试，0–5000毫秒，最多同时两次；返回服务端实际等待时间。",
                 annotations=_READ_ONLY)
    async def vococo_delay(
        delay_ms: Annotated[int, Field(strict=True, ge=0, le=5000)] = 1000,
    ) -> dict[str, str | int | float]:
        if slots.locked():
            raise ValueError("等待测试忙，请稍后重试")
        async with slots:
            started = time.perf_counter()
            await asyncio.sleep(delay_ms / 1000)
            return _receipt("vococo_delay", requested_delay_ms=delay_ms,
                            server_elapsed_ms=round((time.perf_counter() - started) * 1000, 3))


def _register_long_reply(server: FastMCP) -> None:
    @server.tool(description="生成256–32768字符中英混合测试正文，含头尾标记、字符/字节数及SHA256；不读取文件。",
                 annotations=_READ_ONLY)
    def vococo_long_reply(
        characters: Annotated[int, Field(strict=True, ge=256, le=32768)] = 4096,
    ) -> dict[str, str | int | float]:
        started = time.perf_counter()
        result = _receipt("vococo_long_reply", requested_characters=characters)
        receipt = result["receipt"]
        prefix, suffix = f"BEGIN_{receipt}\n", f"\nEND_{receipt}"
        pattern = "这是连接测试数据，不是任务结果。MCP latency and long reply test.\n"
        remaining = characters - len(prefix) - len(suffix)
        text = prefix + (pattern * (remaining // len(pattern) + 1))[:remaining] + suffix
        metrics = {"characters": len(text), "utf8_bytes": len(text.encode("utf-8")),
                   "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "server_elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        _LOG.info("probe_result %s", json.dumps({"receipt": receipt, **metrics}))
        return {**result, **metrics, "text": text}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8852)
    parser.add_argument("--public-origin", type=public_origin)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        server = create_server(args.port, args.public_origin)
    except ValueError as error:
        parser.error(str(error))
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
