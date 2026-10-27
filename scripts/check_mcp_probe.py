"""小样本 MCP 连通/耗时/长回复验收，不是负载测试，也不测 ChatGPT 模型耗时。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _milliseconds(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _call(session: ClientSession, name: str, arguments: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    response = await session.call_tool(name, arguments)
    elapsed = _milliseconds(started)
    if response.isError or not response.structuredContent:
        raise RuntimeError(f"{name} 未返回有效结构化结果: {response.content}")
    return response.structuredContent, elapsed


def _validate_text(result: dict, characters: int) -> None:
    text = result["text"]
    assert len(text) == result["characters"] == characters, "长回复字符数不符"
    assert len(text.encode("utf-8")) == result["utf8_bytes"], "长回复字节数不符"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == result["sha256"], "长回复哈希不符"
    assert text.startswith("BEGIN_" + result["receipt"]), "长回复开头不符"
    assert text.endswith("END_" + result["receipt"]), "长回复尾部缺失"


async def _measure_calls(session: ClientSession, samples: int) -> dict:
    pings, long_replies = [], []
    for index in range(samples):
        result, elapsed = await _call(session, "vococo_ping", {"challenge": f"speed_{index}"})
        pings.append({"elapsed_ms": elapsed, "receipt": result["receipt"]})
    for characters in [1024, 8192, 32768]:
        result, elapsed = await _call(session, "vococo_long_reply", {"characters": characters})
        _validate_text(result, characters)
        long_replies.append({key: result[key] for key in [
            "characters", "utf8_bytes", "receipt", "sha256", "server_elapsed_ms",
        ]} | {"elapsed_ms": elapsed, "integrity_ok": True})
    result, elapsed = await _call(session, "vococo_delay", {"delay_ms": 2000})
    timings = [row["elapsed_ms"] for row in pings]
    return {"ping_samples": pings, "ping_ms": {
        "min": min(timings), "median": round(statistics.median(timings), 2), "max": max(timings),
    }, "long_replies": long_replies, "delay": {
        **result, "elapsed_ms": elapsed,
        "client_minus_server_ms": round(elapsed - result["server_elapsed_ms"], 2),
    }}


async def _measure_nonblocking(session: ClientSession) -> dict:
    pending = asyncio.create_task(_call(session, "vococo_delay", {"delay_ms": 1500}))
    try:
        await asyncio.sleep(0.25)
        ping, ping_ms = await _call(session, "vococo_ping", {"challenge": "nonblocking"})
        delay_was_pending = not pending.done()
        delayed, delay_ms = await pending
        return {"ping_ms": ping_ms, "delay_ms": delay_ms,
                "ping_receipt": ping["receipt"], "delay_receipt": delayed["receipt"],
                "delay_still_pending_when_ping_returned": delay_was_pending}
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def check(url: str, samples: int = 5, use_env_proxy: bool = False) -> dict:
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30, trust_env=use_env_proxy) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                initialized_ms = _milliseconds(started)
                tools = [tool.name for tool in (await session.list_tools()).tools]
                result = await _measure_calls(session, samples)
                result["nonblocking"] = await _measure_nonblocking(session)
    return {"url": url, "initialize_ms": initialized_ms, "tools": tools, **result,
            "scope": "MCP客户端往返耗时；不含ChatGPT工具选择、模型生成或语音播放耗时"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="实际 MCP URL，例如 http://127.0.0.1:8852/mcp")
    parser.add_argument("--samples", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--use-env-proxy", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(check(args.url, args.samples, args.use_env_proxy))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
