"""真实联网研究验收；显式传认证文件，全部状态写独立目录，不触碰生产数据库。

uv run python tests/manual/research_smoke.py --env-file /path/to/.env
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv


async def main(arguments):
    load_dotenv(arguments.env_file, override=True)
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from vococo import config
    from vococo.core import research, task_runner, tasks
    from vococo.gateway import settings_store, task_routes

    if arguments.settings_file:
        settings_store._PATH = Path(arguments.settings_file)
    root = Path(__file__).resolve().parents[2]
    target = root / "data" / "research-validation" / str(int(time.time()))
    target.mkdir(parents=True)
    config.DATA_DIR = target
    config.AI_BRAIN_DIR = target / "empty-brain"
    config.AGENT_WORKSPACES_DIR = target / "workspaces"
    config.WEB_AUTH_TOKEN = ""
    tasks._DB = None
    app = web.Application()
    task_routes.register_routes(app)
    start = time.monotonic()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/research?session_key=smoke", json={
            "title": "研究能力真实验收", "model": arguments.model,
            "prompt": arguments.question,
            "budget": {"seconds": 600, "searches": 8, "fetches": 12,
                       "tokens": 180000, "max_turns": 20},
        })
        row = await response.json()
        if response.status != 202:
            raise RuntimeError(row)
        task_id = row["id"]
        print(json.dumps({"task_id": task_id, "output": str(target)}, ensure_ascii=False), flush=True)
        previous = ""
        while True:
            row = await (await client.get(f"/tasks/{task_id}")).json()
            if row["progress_note"] != previous:
                previous = row["progress_note"]
                print(f"{time.monotonic() - start:.1f}s {row['status']}: {previous}", flush=True)
            if row["status"] in tasks.TERMINAL_STATUSES:
                break
            await asyncio.sleep(1)
        state = await (await client.get(f"/tasks/{task_id}/research")).json()
        (target / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        report = await client.get(f"/tasks/{task_id}/report")
        if report.status == 200:
            (target / "report.md").write_text(await report.text())
        summary = {"status": row["status"], "error": state["error"],
                   "summary": row["result_summary"], "usage": state["usage"],
                   "sources": [{"id": item["id"], "url": item["url"],
                                "claims": len(item["claims"])} for item in state["sources"]],
                   "checkpoints": list(state["checkpoints"]),
                   "report_http_status": report.status,
                   "elapsed_seconds": round(time.monotonic() - start, 1)}
        (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if row["status"] != "done":
            raise RuntimeError("真实研究未通过，详见隔离输出目录")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--settings-file", help="只读供应商配置文件；不会复制或改写其中密钥")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--question", default=(
        "比较 GPT Researcher 与 DeerFlow 当前项目定位和许可证，评估为一个已有 Python "
        "Claude Agent SDK 个人助手增加深度研究，选嵌入研究组件还是引入整套平台。"
        "限定公共官方资料，至少读取两个独立官方页面；优先 GitHub README 和项目官方文档。"
        "最多800字中文，区分事实与推断，包含反例、未知项和可点击出处；不依赖未经证实的费用宣传。"))
    asyncio.run(main(parser.parse_args()))
