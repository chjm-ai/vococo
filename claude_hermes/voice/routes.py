"""语音模式的 aiohttp 路由:stt + 对话 SSE + 停止 + 任务板。

鉴权:web.py 的 _security_mw 只管跨源拦截和安全头,真正校验 WEB_AUTH_TOKEN 的是各
handler 自己(见 web.py 的 _ok_token/_guard)。这里复刻同一形态,主界面里的通话视图
复用登录后存在 localStorage 的同一枚 token(同源共享),不必再单独登录一次。
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
from pathlib import Path

from aiohttp import web

from .. import config
from ..core.agent import Done, TextDelta, ToolStarted
from . import executor, notify, prompts, session, stt, task_tools, tasks, tts, ws

_STATIC = Path(__file__).resolve().parent / "static"

# 同一时刻只允许一轮语音对话在跑;stop 通过它通知正在跑的那一轮别再合成音频了。
_lock = asyncio.Lock()
_stop_event: asyncio.Event | None = None


def _ok_token(request: web.Request) -> bool:
    if not config.WEB_AUTH_TOKEN:
        return True
    # /voice/tasks/stream 用浏览器原生 EventSource,不能带自定义 header,只能走 query 参数。
    tok = request.headers.get("X-Auth-Token") or request.query.get("token") or ""
    return hmac.compare_digest(tok, config.WEB_AUTH_TOKEN)


def _guard(request: web.Request) -> web.Response | None:
    if not _ok_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


async def _handle_page_gone(request: web.Request) -> web.Response:
    # 独立 /voice 页已退休(2026-07-09 起语音通话并入主 SPA 的通话视图,见
    # gateway/adapters/web_static/index.html 的 #callView);旧书签/主屏快捷方式
    # 一律回落主界面。
    raise web.HTTPFound("/")


async def _handle_config(request: web.Request) -> web.Response:
    return web.json_response({"enabled": True})


async def _handle_stt(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    audio, filename, ctype = await stt.read_audio(request)
    if not audio:
        return web.json_response({"error": "没收到音频"}, status=400)
    text, err = await stt.transcribe(audio, filename, ctype)
    if text is None:
        return web.json_response({"error": err}, status=502)
    return web.json_response({"text": text})


def _sse(resp: web.StreamResponse, event: str, payload: dict) -> "asyncio.Future":
    data = json.dumps(payload, ensure_ascii=False)
    return resp.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))


async def _handle_send(request: web.Request) -> web.StreamResponse:
    """发起一轮:body 既可以是 {"text": "..."} 也可以直接传 multipart 音频
    (字段名 audio)——后者省掉「先 /voice/stt 拿文字、再单独发一次 /voice/send」
    这一趟完整的网络往返(手机 ⇄ 服务器隔着隧道,这一趟在真机上很有分量),
    服务端转写完直接续跑同一个 SSE 流,先吐一个 event:transcript 回显文字。
    """
    global _stop_event
    if (g := _guard(request)) is not None:
        return g
    is_audio = (request.content_type or "").startswith("multipart/")
    audio = filename = actype = None
    user_text = ""
    if is_audio:
        audio, filename, actype = await stt.read_audio(request)
        if not audio:
            return web.json_response({"error": "没收到音频"}, status=400)
    else:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        user_text = (body.get("text") or "").strip()
        if not user_text:
            return web.json_response({"error": "text 不能为空"}, status=400)
    if _lock.locked():
        return web.json_response({"error": "上一轮还没说完"}, status=409)

    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)

    async with _lock:
        stop_event = asyncio.Event()
        _stop_event = stop_event
        t0 = time.monotonic()
        splitter = tts.SentenceSplitter()
        full_text = ""
        filler_sent = False
        t_first_text = t_first_audio = 0.0
        try:
            if is_audio:
                text, err = await stt.transcribe(audio, filename, actype)
                if text is None:
                    await _sse(resp, "done", {"full_text": "", "error": err})
                    return resp
                user_text = text
                await _sse(resp, "transcript", {"text": user_text})
            prompt_text = prompts.build_prompt(user_text)
            async for ev in session.run_turn(prompt_text, extra_mcp_servers=task_tools.build_server()):
                if isinstance(ev, ToolStarted):
                    # 干活垫话(F10):本轮第一次顶层工具调用时插一声科技感音效,暗示"正在处理",
                    # 不等模型自己开口;parent_id 非空是子代理内部的工具,不算——只在最外层触发一次。
                    if not filler_sent and ev.parent_id is None and not stop_event.is_set():
                        filler_sent = True
                        audio_bytes = await tts.filler_audio()
                        payload = {
                            "audio_b64": base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else None,
                        }
                        await _sse(resp, "filler", payload)
                elif isinstance(ev, TextDelta):
                    if not t_first_text:
                        t_first_text = time.monotonic()
                    full_text += ev.text
                    await _sse(resp, "text", {"text": ev.text})
                    if not stop_event.is_set():
                        for sentence in splitter.feed(ev.text):
                            await _emit_sentence(resp, sentence)
                            if not t_first_audio:
                                t_first_audio = time.monotonic()
                elif isinstance(ev, Done):
                    if not stop_event.is_set():
                        for sentence in splitter.flush():
                            await _emit_sentence(resp, sentence)
                            if not t_first_audio:
                                t_first_audio = time.monotonic()
                    reply = ev.reply
                    session.append(user_text, reply.text)
                    if reply.sdk_session_id:
                        session.set_resume(reply.sdk_session_id)
                    await _sse(resp, "done", {"full_text": reply.text or full_text})
                    print(
                        f"[voice/send] first_text={t_first_text - t0:.2f}s "
                        f"first_audio={(t_first_audio - t0) if t_first_audio else -1:.2f}s "
                        f"total={time.monotonic() - t0:.2f}s",
                        flush=True,
                    )
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 —— 兜底:出错也要给前端一个 done,不让它干等
            try:
                await _sse(resp, "done", {"full_text": full_text, "error": str(exc)})
            except (ConnectionResetError, RuntimeError):
                pass
        finally:
            if _stop_event is stop_event:
                _stop_event = None
    return resp


async def _emit_sentence(resp: web.StreamResponse, sentence: str) -> None:
    audio = await tts.synthesize(sentence, config.VOICE_TTS_VOICE)
    payload = {"text": sentence, "audio_b64": base64.b64encode(audio).decode("ascii") if audio else None}
    await _sse(resp, "sentence", payload)


async def _handle_stop(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    if _stop_event is not None:
        _stop_event.set()
    return web.json_response({"ok": True})


# ── P1 任务板:列表/详情/停止/在线播报的常驻 SSE(F8/F10) ─────────────────────
async def _handle_tasks_list(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    return web.json_response(tasks.list_recent())


async def _handle_task_detail(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    task = tasks.get(request.match_info["task_id"])
    if task is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(task)


async def _handle_task_stop(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    ok = executor.cancel(request.match_info["task_id"])
    return web.json_response({"ok": ok})


async def _handle_tasks_stream(request: web.Request) -> web.StreamResponse:
    """常驻 SSE:/voice 页面开着就订阅它,后台任务终态时收到 event:task_done(F8)。"""
    if (g := _guard(request)) is not None:
        return g
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    q = notify.subscribe()
    try:
        while True:
            try:
                event, payload = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                await resp.write(b": keep-alive\n\n")  # 防中间代理/隧道空闲断连
                continue
            await _sse(resp, event, payload)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        notify.unsubscribe(q)
    return resp


def register_routes(app: web.Application) -> None:
    """挂载语音模式全部路由。仅当 config.VOICE_ENABLED 时,web.py 才会调用本函数。"""
    app.add_routes(
        [
            web.get("/voice", _handle_page_gone),
            web.get("/voice/config", _handle_config),
            web.post("/voice/stt", _handle_stt),
            web.post("/voice/send", _handle_send),
            web.post("/voice/stop", _handle_stop),
            web.get("/voice/tasks", _handle_tasks_list),
            web.get("/voice/tasks/stream", _handle_tasks_stream),
            web.get("/voice/tasks/{task_id}", _handle_task_detail),
            web.post("/voice/tasks/{task_id}/stop", _handle_task_stop),
        ]
    )
    if config.VOICE_WS_ENABLED:
        app.add_routes([web.get("/voice/ws", ws.handle)])
        # VAD 库文件(vad-web + onnxruntime-web + 手写的 pcm-forwarder-worklet.js)
        # 是公开静态资源(不含用户数据),不用 token 校验;用 aiohttp 自带的目录
        # 静态服务而不是逐个手写路由——量大(vad/ 下好几个大文件)且天然支持
        # Range 请求,10MB 的 wasm 在手机弱网下能续传。aiohttp 自带 ETag/
        # Last-Modified 协商缓存,这批文件版本固定不太会变,够用。
        app.router.add_static("/voice/static/", _STATIC, show_index=False)
    asyncio.ensure_future(executor.heal_after_restart())  # F11:重启自愈,一次性
