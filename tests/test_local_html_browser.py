"""本地 HTML 链接使用真实文件、接口与浏览器，不调用模型。"""
import asyncio
import os
from pathlib import Path

import pytest

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store
from test_library_integration import _browser_environment


@pytest.mark.anyio
async def test_local_html_links_browser(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "local-html-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    root = isolated / "html-link-e2e"
    root.mkdir()
    (root / "page.html").write_text('<h1>本地 HTML 卡片</h1><script>parent.LOCAL_ATTACK=1</script>'
                                   '<img src="https://local-blocked.invalid/a">'
                                   '<a href="https://local-blocked.invalid/go">外链</a>' + '<p>长内容</p>' * 100)
    (root / "c.html").write_text("<h1>C 的新内容</h1>")
    (root / "large.html").write_bytes(b"x" * 200001)
    (root / "bad.html").write_bytes(b"\xff")
    (root / "notes.md").write_text("# 普通文档")
    (root / "会议纪要 空格.md").write_text("# 会议纪要正文")
    (root / "signed.txt").write_text("署名逐字稿正文")
    (isolated / "outside.html").write_text("<p>目录外</p>")
    from vococo.core import tasks
    from vococo.memory import agents

    monkeypatch.setattr(tasks, "_DB", None)
    output = isolated / "业务成果"
    report = output / "项目/知识库与Skill实验（20261027）/结果对照/结果对比.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 正式实验结果", encoding="utf-8")
    agent = agents.create("实验 Agent", str(output))
    task = tasks.create("知识实验", "生成报告", agent_id=agent["id"])
    task_conv = tasks.session_key(task["id"])
    task_turn = session_store.start_turn(task_conv, "查看结果")
    customer = output / "客户/Minecraft教育英语（客户待确认）/现状.md"
    customer.parent.mkdir(parents=True)
    customer.write_text("# 客户前期评估", encoding="utf-8")
    session_store.finish_turn(task_turn, "[实验结果](项目/知识库与Skill实验（20261027）/结果对照/结果对比.md)\n\n"
                              "`业务成果/客户/Minecraft教育英语（客户待确认）/现状.md`")
    original_cwd = WebAdapter._conv_cwd
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(root) if conv == "html-link-e2e" else original_cwd(self, conv))
    key = config.resolve_session_key("web", "html-link-e2e")
    tid = session_store.start_turn(key, "生成 HTML")
    session_store.finish_turn(tid, f"[历史 HTML]({root / 'page.html'})")
    adapter = WebAdapter()
    screenshots = Path(os.environ.get("PREVIEW_SCREENSHOTS", str(isolated)))
    screenshots.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("local_html_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, str(root), str(screenshots), task_conv, env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 100)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        print(stdout.decode())
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if adapter._runner:
            await adapter._runner.cleanup()
