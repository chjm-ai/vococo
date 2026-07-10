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
from ..core.agent import Done, TextDelta
from . import executor, notify, omni_realtime, prompts, session, stt, task_tools, tasks, tts, ws

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
    return web.json_response({
        "enabled": True,
        "omni_enabled": config.VOICE_OMNI_ENABLED,
        # Omni WebRTC 链路的 turn_detection 用,不能吃阿里云的默认值(更灵敏,会把
        # 一句话中间的停顿误判成说完)。silence_ms 用 Omni 专属的更长默认值(见
        # config.VOICE_OMNI_VAD_SILENCE_MS 的注释),跟旧 ws.py 链路调优的值分开。
        "vad_threshold": config.VOICE_VAD_THRESHOLD,
        "vad_silence_ms": config.VOICE_OMNI_VAD_SILENCE_MS,
    })


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
        print(f"[voice/send] 收到文字直发: {user_text[:50]!r}", flush=True)
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
        t_first_text = t_first_audio = 0.0
        # 2026-07-10:句子合成从内联 await 改成后台并行(同 ws.py 的 _emit_sentence/
        # _sentence_sender 套路)——之前合成一句要几百毫秒到一秒多,这段时间完全没在
        # 继续消费 session.run_turn() 吐出来的下一段 TextDelta,句子越多累积延迟越
        # 明显(用户反馈"文字很快,语音播放慢")。现在合成和"继续读模型输出"并行,
        # 发给客户端的顺序仍然靠 sentence_queue 保证是 seq 递增。
        sentence_queue: "asyncio.Queue" = asyncio.Queue()
        pending_synth_tasks: list = []
        # first_audio_ts 是个一格的容器,给 _sentence_sender 回填"第一句音频真正
        # 发出去"的时刻——不能在 _emit_sentence(入队时)记,那记的是排队时刻,并行
        # 化之后基本等于文字时刻,measure 不出优化到底省了多少。
        first_audio_ts: list = []
        sender_task = asyncio.ensure_future(_sentence_sender(resp, sentence_queue, first_audio_ts))
        seq = 0
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
                if isinstance(ev, TextDelta):
                    if not t_first_text:
                        t_first_text = time.monotonic()
                    full_text += ev.text
                    await _sse(resp, "text", {"text": ev.text})
                    if not stop_event.is_set():
                        for sentence in splitter.feed(ev.text):
                            seq = _emit_sentence(sentence_queue, pending_synth_tasks, seq, sentence)
                elif isinstance(ev, Done):
                    if not stop_event.is_set():
                        for sentence in splitter.flush():
                            seq = _emit_sentence(sentence_queue, pending_synth_tasks, seq, sentence)
                    # 告诉 sender 协程"没有下一句了",等它把队列里剩下还在后台合成中
                    # 的句子按顺序发完,再继续走 done——不然客户端可能先收到 done、
                    # 后面才姗姗来迟几句 sentence,顺序就乱了。
                    sentence_queue.put_nowait(None)
                    await sender_task
                    t_first_audio = first_audio_ts[0] if first_audio_ts else 0.0
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
            if not sender_task.done():
                sender_task.cancel()
            for t in pending_synth_tasks:
                t.cancel()
            if _stop_event is stop_event:
                _stop_event = None
    return resp


def _emit_sentence(queue: "asyncio.Queue", pending: list, seq: int, sentence: str) -> int:
    """把这句话的 TTS 合成丢到后台并发跑,不阻塞正在读的 Claude 文字流,见调用处注释。"""
    synth_task = asyncio.ensure_future(tts.synthesize(sentence, config.VOICE_TTS_VOICE))
    pending.append(synth_task)
    queue.put_nowait((seq, sentence, synth_task))
    return seq + 1


async def _sentence_sender(resp: web.StreamResponse, queue: "asyncio.Queue", first_audio_ts: list) -> None:
    """按 seq 顺序把后台并行合成好的 TTS 音频依次发给客户端——合成并行,发送顺序仍然线性。"""
    while True:
        item = await queue.get()
        if item is None:
            return
        _seq, sentence, synth_task = item
        try:
            audio = await synth_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 —— 单句合成失败不该拖垮整轮,跳过继续发下一句
            audio = None
        payload = {"text": sentence, "audio_b64": base64.b64encode(audio).decode("ascii") if audio else None}
        await _sse(resp, "sentence", payload)
        if not first_audio_ts:
            first_audio_ts.append(time.monotonic())


async def _handle_stop(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    if _stop_event is not None:
        _stop_event.set()
    return web.json_response({"ok": True})


async def _handle_clear(request: web.Request) -> web.Response:
    """清空语音聊天上下文(通话界面顶部的橡皮擦按钮):清的是 voice-chat:main,
    跟聊天页的 /clear 各管各的。"""
    if (g := _guard(request)) is not None:
        return g
    session.clear()
    return web.json_response({"ok": True})


# ── P3 阶段二:Qwen-Omni-Realtime 的 WebRTC 信令代理 ──────────────────────────
async def _handle_omni_webrtc(request: web.Request) -> web.Response:
    """代理浏览器的 SDP offer 换 answer(见 omni_realtime.exchange_webrtc_sdp
    顶部注释——浏览器自己既过不了跨域,也不能拿到真实 API key,必须后端代理)。"""
    if (g := _guard(request)) is not None:
        return g
    offer_sdp = await request.text()
    if not offer_sdp.strip():
        return web.json_response({"error": "缺少 SDP offer"}, status=400)
    print(f"[voice/omni] 收到 SDP offer({len(offer_sdp)} 字节),转发给阿里云…", flush=True)
    try:
        answer_sdp = await omni_realtime.exchange_webrtc_sdp(offer_sdp)
    except RuntimeError as exc:
        print(f"[voice/omni] 信令交换失败: {exc}", flush=True)
        return web.json_response({"error": str(exc)}, status=502)
    print("[voice/omni] 信令交换成功,已把 answer 返回给浏览器", flush=True)
    return web.Response(text=answer_sdp, content_type="application/sdp")


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
            web.post("/voice/clear", _handle_clear),
            web.get("/voice/tasks", _handle_tasks_list),
            web.get("/voice/tasks/stream", _handle_tasks_stream),
            web.get("/voice/tasks/{task_id}", _handle_task_detail),
            web.post("/voice/tasks/{task_id}/stop", _handle_task_stop),
            web.post("/voice/omni/webrtc", _handle_omni_webrtc),
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
