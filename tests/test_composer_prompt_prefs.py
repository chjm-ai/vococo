"""提示词偏好保存不能覆盖同一分类的并发更新。"""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_composer_prompt_prefs_reject_stale_updates(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "prompt-prefs-test")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/prefs", adapter._handle_prefs_get), web.patch("/prefs", adapter._handle_pref_update)])
    auth = {"X-Auth-Token": config.WEB_AUTH_TOKEN}
    key = "composer_prompts_thinking"
    edits = [json.dumps([["测试", body]], ensure_ascii=False) for body in ("第一版", "第二版")]

    async with TestClient(TestServer(app)) as client:
        async def save(value, expected=None, headers=auth):
            return await client.patch("/prefs", headers=headers, json={"key": key, "expected": expected, "value": value})

        assert (await save(edits[0], headers={})).status == 401
        assert (await save([])).status == 400
        assert (await save(edits[0], expected=0)).status == 400

        first, second = await asyncio.gather(*(save(value) for value in edits))
        assert sorted((first.status, second.status)) == [200, 409]
        winner, conflict = (edits[0], second) if first.ok else (edits[1], first)
        assert await conflict.json() == {"current": winner}
        assert (await save("[]", expected=winner)).ok
        assert (await save(winner, expected=winner)).status == 409
        prefs = await client.get("/prefs", headers=auth)
        assert (await prefs.json())[key] == "[]"
