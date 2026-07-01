"""clarify 底座 + ask_user 工具:登记/解除/超时/按钮/文字回复/完整 round-trip。"""
from __future__ import annotations

import anyio
import pytest


@pytest.fixture(autouse=True)
def _clean_clarify():
    """clarify 状态是模块级,用例间清干净。"""
    from claude_hermes.gateway import clarify

    clarify._pending.clear()
    clarify._by_session.clear()
    yield
    clarify._pending.clear()
    clarify._by_session.clear()


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_resolve_then_wait_returns_answer():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", ["A", "B"])
        clarify.resolve(p.clarify_id, "A")
        return await clarify.wait(p.clarify_id, timeout=1)

    assert anyio.run(go) == "A"


def test_wait_timeout_returns_none():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", [])
        return await clarify.wait(p.clarify_id, timeout=0.05)

    assert anyio.run(go) is None


def test_button_index_maps_to_choice():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", ["苹果", "香蕉"])
        clarify.resolve_button(p.clarify_id, "1")  # 0-based → 香蕉
        return await clarify.wait(p.clarify_id, timeout=1)

    assert anyio.run(go) == "香蕉"


def test_text_reply_resolves_session():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", [])  # 开放式
        clarify.resolve_text_for_session("s1", "我的答案")
        return await clarify.wait(p.clarify_id, timeout=1)

    assert anyio.run(go) == "我的答案"


def test_text_reply_coerces_number_to_choice():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", ["红", "绿", "蓝"])
        clarify.resolve_text_for_session("s1", "3")  # 1-based 序号 → 蓝
        return await clarify.wait(p.clarify_id, timeout=1)

    assert anyio.run(go) == "蓝"


def test_clear_session_cancels():
    from claude_hermes.gateway import clarify

    async def go():
        p = clarify.register("s1", [])
        holder = {}
        async with anyio.create_task_group() as tg:

            async def w():
                holder["ans"] = await clarify.wait(p.clarify_id, timeout=2)

            tg.start_soon(w)
            await anyio.sleep(0.02)  # 让 wait 先进入阻塞
            holder["n"] = clarify.clear_session("s1")
        return holder["n"], holder["ans"]

    n, ans = anyio.run(go)
    assert n == 1 and ans == ""  # 阻塞中被取消 → 空串


def test_ask_user_no_context_graceful():
    from claude_hermes.tools import builtin

    async def go():
        return await builtin.ask_user.handler({"question": "在吗?"})

    assert "不支持交互" in _text(anyio.run(go))


def test_ask_user_full_roundtrip():
    """set_current → ask_user 弹按钮阻塞 → 模拟点按钮 → 工具拿到答案。"""
    from claude_hermes.gateway import clarify
    from claude_hermes.tools import builtin

    sent = []

    class FakeAdapter:
        platform = "telegram"

        async def present_choice(self, chat_id, choice):
            sent.append(choice)

        async def send(self, chat_id, text):
            sent.append(text)

    async def go():
        token = clarify.set_current("s1", FakeAdapter(), 123)
        holder = {}
        async with anyio.create_task_group() as tg:

            async def call():
                holder["r"] = await builtin.ask_user.handler(
                    {"question": "选哪个?", "options": ["A", "B"]}
                )

            tg.start_soon(call)
            # 等工具把问题发出(pending 出现),模拟用户点第 2 个按钮
            for _ in range(100):
                if clarify.has_pending("s1"):
                    break
                await anyio.sleep(0.01)
            pend = clarify._oldest("s1")
            clarify.resolve_button(pend.clarify_id, "1")
        clarify.reset_current(token)
        return holder["r"]

    result = anyio.run(go)
    assert "用户回答:B" in _text(result)
    assert sent and hasattr(sent[0], "options")  # 确实弹了 Choice 按钮
