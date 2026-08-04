"""文档预览分屏的接口:GET /doc/preview(见与用户的设计讨论——聊天里的文档链接/
工具卡文件路径点击后右侧滑出预览,本地文件这条路径需要后端读文件内容出来)。

_conv_cwd 直接打桩成一个临时目录,不去绕 session_store 的项目哈希映射——
这里只测路径安全校验(越界拒绝)和按后缀/大小分支的行为,不是测项目绑定逻辑本身。
Path.home() 也打桩到隔离目录:不打桩的话"HOME 边界"这条新逻辑会跟着跑测试那台
机器的真实 home 走,谁的开发机上恰好有同名文件就可能出现假阳性/不确定的结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_home(isolated, monkeypatch):
    home = isolated / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def doc_root(fake_home):
    root = fake_home / "project"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def doc_app(isolated, doc_root, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(doc_root))
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/doc/preview", adapter._handle_doc_preview)])
    return app


async def _get(app, path):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        body = await resp.read()
        return resp.status, body, resp.headers


@pytest.mark.anyio
async def test_reads_text_file_in_conv_cwd(doc_app, doc_root):
    (doc_root / "report.md").write_text("# 标题\n正文", encoding="utf-8")

    status, body, headers = await _get(doc_app, "/doc/preview?conv=x&path=report.md")

    assert status == 200
    assert body.decode("utf-8") == "# 标题\n正文"
    assert "text" in headers["Content-Type"]


@pytest.mark.anyio
async def test_reads_absolute_path_inside_cwd(doc_app, doc_root):
    (doc_root / "notes.txt").write_text("hi", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(doc_root / "notes.txt"))

    assert status == 200
    assert body == b"hi"


@pytest.mark.anyio
async def test_rejects_path_traversal(doc_app, doc_root):
    secret = doc_root.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=../secret.txt")

    assert status == 404


@pytest.mark.anyio
async def test_rejects_absolute_path_outside_cwd(doc_app, isolated):
    secret = isolated / "secret.txt"  # isolated(tmp_path)是 fake_home 的兄弟目录,不在任何边界内
    secret.write_text("do not leak", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(secret))

    assert status == 404


@pytest.mark.anyio
async def test_missing_file_404(doc_app):
    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=nope.md")

    assert status == 404


@pytest.mark.anyio
async def test_oversized_file_413(doc_app, doc_root, monkeypatch):
    from vococo.gateway.adapters import web as web_module

    monkeypatch.setattr(web_module, "_DOC_PREVIEW_MAX", 10)
    (doc_root / "big.txt").write_text("x" * 100, encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=big.txt")

    assert status == 413


@pytest.mark.anyio
async def test_pdf_gets_pdf_content_type(doc_app, doc_root):
    (doc_root / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

    status, _body, headers = await _get(doc_app, "/doc/preview?conv=x&path=doc.pdf")

    assert status == 200
    assert headers["Content-Type"] == "application/pdf"


@pytest.mark.anyio
async def test_requires_auth_token_when_configured(isolated, doc_root, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "s3cr3t")
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(doc_root))
    (doc_root / "a.txt").write_text("hi", encoding="utf-8")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/doc/preview", adapter._handle_doc_preview)])

    status, _body, _headers = await _get(app, "/doc/preview?conv=x&path=a.txt")

    assert status == 401


# ── AI_BRAIN 兜底根目录:非项目会话下 AI 常把笔记直接写进 Obsidian vault(比如
# "00-inbox/xxx.md" 这种收件箱惯例),不在会话 cwd 范围内——不是"公网访问不到本地
# 文件"(HTTP 请求本来就是服务端执行,客户端在哪没区别),原来只认会话 cwd 会把这类
# 合法文件误判成"越界"拒绝,见与用户的讨论。 ──────────────────────────────────


@pytest.mark.anyio
async def test_falls_back_to_ai_brain_when_not_in_conv_cwd(doc_app):
    inbox = config.AI_BRAIN_DIR / "00-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "note.md").write_text("笔记正文", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=00-inbox/note.md")

    assert status == 200
    assert body.decode("utf-8") == "笔记正文"


@pytest.mark.anyio
async def test_conv_cwd_wins_over_ai_brain_on_name_collision(doc_app, doc_root):
    (doc_root / "same.md").write_text("项目里的版本", encoding="utf-8")
    (config.AI_BRAIN_DIR).mkdir(parents=True, exist_ok=True)
    (config.AI_BRAIN_DIR / "same.md").write_text("AI_BRAIN 里的版本", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=same.md")

    assert status == 200
    assert body.decode("utf-8") == "项目里的版本"


@pytest.mark.anyio
async def test_ai_brain_fallback_still_rejects_traversal(doc_app):
    secret = config.AI_BRAIN_DIR.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")
    config.AI_BRAIN_DIR.mkdir(parents=True, exist_ok=True)

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=../secret.txt")

    assert status == 404


# ── HOME 兜底边界:窄白名单(只认会话 cwd/AI_BRAIN)会把 Desktop/Documents 这类 AI
# 常见落笔位置全部挡在外面,误报"文件不存在"(2026-07-30 用户反馈基本没有能预览成功
# 的例子)。改成"只要不越出 HOME"——这是单用户私人助理机器,agent 本来就能用
# Bash/Write 碰到 HOME 下任何文件,这里的边界检查是额外防护,不是也没必要比 agent
# 自身权限更严。──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_reads_absolute_path_under_home_outside_conv_and_brain(doc_app, fake_home):
    desktop = fake_home / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    (desktop / "poster.html").write_text("<h1>poster</h1>", encoding="utf-8")

    status, body, headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(desktop / "poster.html"))

    assert status == 200
    assert body.decode("utf-8") == "<h1>poster</h1>"
    assert "text/html" in headers["Content-Type"]


@pytest.mark.anyio
async def test_rejects_absolute_path_outside_home(doc_app, isolated):
    outside = isolated / "outside.txt"  # isolated(tmp_path)跟 fake_home 是兄弟目录,不在 HOME 下
    outside.write_text("do not leak", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(outside))

    assert status == 404


# ── 模糊兜底搜索:AI 提到项目文件时经常把包名前缀说漏(比如把 vococo/memory/
# images.py 说成 memory/images.py),直接拼接找不到就该按路径尾部搜一遍,而不是让用户
# 点开一堆"文件不存在"——见与用户的讨论(2026-07-31,原话"针对这个问题,我们有两种解决
# 思路"，选的是让它尽量能正确识别,而不是不加超链接)。──────────────────────────────


@pytest.mark.anyio
async def test_fuzzy_resolves_path_missing_package_prefix(doc_app, doc_root):
    pkg = doc_root / "vococo" / "memory"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "images.py").write_text("# real module", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=memory/images.py")

    assert status == 200
    assert body.decode("utf-8") == "# real module"


@pytest.mark.anyio
async def test_fuzzy_search_skips_heavy_dirs(doc_app, doc_root):
    # .git/ 下藏一个同名文件,不该被模糊搜索捞出来(既慢又几乎不会是用户想看的东西)
    trap = doc_root / ".git" / "memory"
    trap.mkdir(parents=True, exist_ok=True)
    (trap / "images.py").write_text("should not be found", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=memory/images.py")

    assert status == 404


@pytest.mark.anyio
async def test_fuzzy_search_requires_at_least_two_segments(doc_app, doc_root):
    # 孤零零一个文件名(没有目录段可"丢")太容易撞同名文件,不猜——即使真的存在也不搜
    nested = doc_root / "a" / "b" / "c"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "lonely.md").write_text("hi", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=lonely.md")

    assert status == 404


@pytest.mark.anyio
async def test_fuzzy_search_prefers_shallowest_match(doc_app, doc_root):
    deep = doc_root / "a" / "b" / "notes" / "x.md"
    deep.parent.mkdir(parents=True, exist_ok=True)
    deep.write_text("deep", encoding="utf-8")
    shallow = doc_root / "c" / "notes" / "x.md"
    shallow.parent.mkdir(parents=True, exist_ok=True)
    shallow.write_text("shallow", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=notes/x.md")

    assert status == 200
    assert body.decode("utf-8") == "shallow"
