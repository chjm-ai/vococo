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
import re
import time
import uuid
from aiohttp import web

from .. import config, providers
from ..core import task_runner, tasks
from ..core.agent import Done, TextDelta, ToolInput, ToolStarted
from . import notify, omni_realtime, prompts, session, stt, task_tools, tts
from ..gateway import clarify, settings_store, task_routes
from ..memory import session_store
from ..tools import selfops
from .adapter import VoiceAdapter

_VOICE_CONFIG_FILE = config.DATA_DIR / "voice_config.json"

# 同一时刻只允许一轮语音对话在跑。每轮带独有 turn_id:挂断时只能取消自己发起的
# 那一轮，迟到的 stop 包不能误杀下一轮。_turn_task 是持锁运行的 HTTP handler，
# 新一句语音仍可抢占旧轮；旧 WS 链路持锁时它为 None，维持原 409 行为。
_lock = asyncio.Lock()
_stop_event: asyncio.Event | None = None
_turn_task: asyncio.Task | None = None
_turn_id: str | None = None
_TURN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _request_turn_id(request: web.Request) -> str:
    """读取前端轮次 ID；旧客户端未传时服务端补一个，保留其原有行为。"""
    turn_id = request.headers.get("X-Voice-Turn-Id", "").strip()
    if turn_id and _TURN_ID_RE.fullmatch(turn_id):
        return turn_id
    return str(uuid.uuid4())


def _cancel_active_turn(turn_id: str | None = None) -> bool:
    """停止当前匹配轮的生成与 TTS；无 ID 的旧 stop 请求只安全地停 TTS。"""
    if turn_id is not None and turn_id != _turn_id:
        return False
    if _stop_event is not None:
        _stop_event.set()
    if turn_id is not None and _turn_task is not None and not _turn_task.done():
        _turn_task.cancel()
        return True
    return _stop_event is not None

# 文本累积器(2026-07-22):当 Omni VAD 将一段长语音腰斩成多段、前端缓冲没及时合并
# 时,后端在抢占路径里合并相邻文本再送 Claude。_prev_text 存上一轮的请求文本,
# _prev_ts 存上一轮启动时刻。两段文字直发请求在 _TEXT_MERGE_WINDOW 秒内到达
# 且锁还被占着时,合并后取消旧轮重新跑,避免丢段+复读。
_prev_text: str = ""
_prev_ts: float = 0.0
_TEXT_MERGE_WINDOW: float = 3.0


# 「后端会自动垫一句等待话术」的真正实现(2026-07-10 前这句承诺只写在 prompts.py
# 的指令块里,代码从没做过——模型被告知"不用自己说等待话术",后端又不垫,结果就是
# 闷头跑工具、用户对着几十秒静音以为掉线)。模型一个字没吐就直接开始调工具时,
# 立即垫一句短话术,让用户知道"听到了,在办"。轮换几句,不至于每次一模一样。
_FILLER_PHRASES = ("我看一下,稍等。", "稍等,我去查查。", "好,这就去办,等我几秒。")
_filler_idx = 0


def _next_filler() -> str:
    global _filler_idx
    _filler_idx = (_filler_idx + 1) % len(_FILLER_PHRASES)
    return _FILLER_PHRASES[_filler_idx]


def _load_voice_config() -> None:
    """从 data/voice_config.json 恢复持久化的音色设置(覆盖 .env 默认值)。
    文件不存在或格式不对则静默忽略,保留 .env 的默认音色。"""
    try:
        raw = _VOICE_CONFIG_FILE.read_text(encoding="utf-8")
        d = json.loads(raw)
        voice = (d.get("omni_voice") or "").strip()
        if voice:
            config.VOICE_OMNI_VOICE = voice
            print(f"[voice/config] 已从 {_VOICE_CONFIG_FILE} 恢复音色: {voice}", flush=True)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_voice_config() -> None:
    """把当前音色写进 data/voice_config.json。"""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _VOICE_CONFIG_FILE.write_text(
            json.dumps({"omni_voice": config.VOICE_OMNI_VOICE}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[voice/config] 持久化音色失败: {exc}", flush=True)


# 模块导入时从磁盘恢复持久化音色
_load_voice_config()


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
        # 前端缓冲强制发送安全网(毫秒),默认 180s:连续长口述的兜底阈值,见
        # config.VOICE_OMNI_SAFETY_MS。
        "safety_ms": config.VOICE_OMNI_SAFETY_MS,
        # Omni 出声模式:session.update 的 voice 字段用。注意跟 VOICE_TTS_VOICE 是
        # 两张音色表,Cherry 在 Omni-Realtime 上会 400(2026-07-10 真机实锤)。
        "omni_voice": config.VOICE_OMNI_VOICE,
    })


async def _handle_config_post(request: web.Request) -> web.Response:
    """修改语音配置(目前只支持 omni_voice)。运行时生效,持久化到
    data/voice_config.json。"""
    if (g := _guard(request)) is not None:
        return g
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)
    voice = (body.get("omni_voice") or "").strip()
    if not voice:
        return web.json_response({"error": "omni_voice 不能为空"}, status=400)
    config.VOICE_OMNI_VOICE = voice
    _save_voice_config()
    print(f"[voice/config] 音色已切换为: {voice}", flush=True)
    return web.json_response({
        "enabled": True,
        "omni_enabled": config.VOICE_OMNI_ENABLED,
        "vad_threshold": config.VOICE_VAD_THRESHOLD,
        "vad_silence_ms": config.VOICE_OMNI_VAD_SILENCE_MS,
        "omni_voice": config.VOICE_OMNI_VOICE,
    })


async def _handle_debug(request: web.Request) -> web.Response:
    """前端语音调试信号上报(2026-07-10 教训:浏览器↔阿里云的 DataChannel 服务端
    完全看不见,连续两次修复都因为拿不到真实证据只能盲改-测-revert。前端把关键
    事件(session/response 生命周期、连接状态转折、打断判定)POST 过来落进服务器
    日志,真机一测日志里就有完整时间线)。只打日志不存库,体积截断防刷爆。"""
    if (g := _guard(request)) is not None:
        return g
    try:
        raw = await request.text()
    except Exception:  # noqa: BLE001
        raw = ""
    print(f"[voice/dbg] {raw[:2000]}", flush=True)
    return web.json_response({"ok": True})


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
    # _prev_text/_prev_ts 缺 global 声明曾是定时炸弹(2026-07-22):函数里对它们有
    # 赋值,Python 会把整个函数内的引用都当局部变量,抢占路径一读就 UnboundLocalError
    # → 新一句直接 500,旧轮也没被取消。
    global _stop_event, _turn_task, _turn_id, _prev_text, _prev_ts
    if (g := _guard(request)) is not None:
        return g
    turn_id = _request_turn_id(request)
    is_audio = (request.content_type or "").startswith("multipart/")
    audio = filename = actype = None
    user_text = ""
    synth_tts = True
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
        # Omni 出声模式(前端把 Claude 的回答转给 Omni 朗读)不需要服务端再合成
        # TTS——sentence 事件照发(前端拿它做逐句朗读的切分),只是不带音频,
        # 省掉每句几百毫秒的合成耗时和 Qwen-TTS 的并发限流风险。
        synth_tts = bool(body.get("tts", True))
        print(f"[voice/send] 收到文字直发: {user_text[:50]!r} tts={synth_tts}", flush=True)
    # 文本累积:两段文字直发(VAD 腰斩)在 3 秒窗口内连续到达时,后段拼回前段再送
    # Claude。2026-07-22 晚修正:合并不再限定"锁还被占着"——前段那轮如果收尾特别
    # 快,锁已释放,后段就会作为全新一轮独立发出,Claude 缺前文,答非所问。窗口从
    # 前段请求"启动时刻"起算,真人两句独立的话极难挤进 3 秒,误拼风险可忽略;
    # 音频请求(带完整整句)不参与合并;干净收尾的轮次会清掉 _prev_text(见 Done 分支)。
    if not is_audio and _prev_text and (time.monotonic() - _prev_ts) < _TEXT_MERGE_WINDOW:
        print(f"[voice/send] 文本累积合并: prev={_prev_text[-30:]!r} + {user_text[:30]!r}", flush=True)
        user_text = _prev_text + user_text
    if _lock.locked():
        t = _turn_task
        if t is None or t.done():
            # 持锁的不是可抢占的 HTTP 轮(旧 WS 链路在跑)——维持原 409。
            return web.json_response({"error": "上一轮还没说完"}, status=409)
        # 抢占:取消旧轮(它的 CancelledError 分支会把半截回复落库),等它释放锁。
        # 15 秒等不到(stream_turn 的取消收尾最多含 5 秒 CLI interrupt)才放弃。
        _cancel_active_turn(_turn_id)
        print("[voice/send] 新一句抢占:已取消上一轮,等待锁释放…", flush=True)
        try:
            await asyncio.wait_for(_lock.acquire(), timeout=15)
        except asyncio.TimeoutError:
            return web.json_response({"error": "上一轮还没说完"}, status=409)
    else:
        try:
            await asyncio.wait_for(_lock.acquire(), timeout=15)
        except asyncio.TimeoutError:  # 极小概率:两句话同时到,锁被同行请求抢先
            return web.json_response({"error": "上一轮还没说完"}, status=409)
    _turn_task = asyncio.current_task()
    _turn_id = turn_id

    # 为下一轮可能的文本累积记录当前请求的文本和时间戳
    if not is_audio:
        _prev_text = user_text
        _prev_ts = time.monotonic()

    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    clarify_token = None
    try:  # 锁已在上面拿到,这里 try/finally 保证释放(含 prepare 失败/return/抢占取消的所有路径)
        await resp.prepare(request)
        voice_adapter = VoiceAdapter(resp)
        clarify_token = clarify.set_current(session.SESSION_KEY, voice_adapter, "main")
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
            # 语音重启后还魂:优先从 GatewayRunner 桥取(见 selfops.save_voice_resume),
            # 再退到文件(纯语音模式无 GatewayRunner)。
            _stored_user: str | None = None
            _voice_bridge = selfops.take_voice_resume()
            if _voice_bridge is not None:
                _resume_data = _voice_bridge["task"]
                _resume_rolled = _voice_bridge["rolled_back"]
            else:
                _resume_data = selfops.consume_resume()
                _resume_rolled = selfops.consume_rollback_flag()
            if _resume_data is not None:
                _resume_prompt = selfops.build_resume_prompt(_resume_data, _resume_rolled)
                _stored_user = selfops.build_resume_store_text(_resume_data, _resume_rolled)
                _has_user_text = bool(user_text and user_text.strip())
                user_text_for_prompt = _resume_prompt
                if _has_user_text:
                    user_text_for_prompt += "\n\n---\n\n用户新消息:\n" + user_text
            else:
                user_text_for_prompt = user_text or ""
            prompt_text = prompts.build_prompt(user_text_for_prompt)
            # 语音通话跟文字聊天走同一套模型选择:本会话已选 > 网页默认 > 全局默认。
            # 语音长期不传 model 参数,导致永远锁死在 config.MODEL(Claude),设置页配了
            # 第三方或文字聊天切过模型都不生效。
            model = session_store.get_chosen_model(session.SESSION_KEY)
            if not model:
                model = settings_store.get_web_default_model() or config.MODEL
                session_store.set_chosen_model(session.SESSION_KEY, model)
            filler_sent = False
            async for ev in session.run_turn(prompt_text, model=model, extra_mcp_servers=task_tools.build_server()):
                if (
                    isinstance(ev, ToolStarted) and ev.parent_id is None
                    and not filler_sent and not full_text and not stop_event.is_set()
                ):
                    # 模型没说话就直接动手调工具 → 垫场话术顶上,见 _FILLER_PHRASES。
                    # 只发 sentence(语音念出来),不发 text——这句不是 Claude 的回答,
                    # 不该出现在聊天气泡里,也不落库。
                    filler_sent = True
                    seq = _emit_sentence(
                        sentence_queue, pending_synth_tasks, seq, _next_filler(), synth_tts, filler=True
                    )
                elif isinstance(ev, ToolInput) and ev.parent_id is None:
                    # 前台轮次的工具动作(查记录/跑脚本)实时推给通话视图的动作行——
                    # ToolInput 才带完整入参,能模板化出"正在执行:git log…"这种细节,
                    # ToolStarted(上面垫话术用的那个)只有工具名。
                    await _sse(resp, "activity", {"text": task_runner.progress_text(ev.name, ev.tool_input)})
                elif isinstance(ev, TextDelta):
                    if not t_first_text:
                        t_first_text = time.monotonic()
                    full_text += ev.text
                    await _sse(resp, "text", {"text": ev.text})
                    if not stop_event.is_set():
                        for sentence in splitter.feed(ev.text):
                            seq = _emit_sentence(sentence_queue, pending_synth_tasks, seq, sentence, synth_tts)
                elif isinstance(ev, Done):
                    if not stop_event.is_set():
                        for sentence in splitter.flush():
                            seq = _emit_sentence(sentence_queue, pending_synth_tasks, seq, sentence, synth_tts)
                    # 告诉 sender 协程"没有下一句了",等它把队列里剩下还在后台合成中
                    # 的句子按顺序发完,再继续走 done——不然客户端可能先收到 done、
                    # 后面才姗姗来迟几句 sentence,顺序就乱了。
                    sentence_queue.put_nowait(None)
                    await sender_task
                    t_first_audio = first_audio_ts[0] if first_audio_ts else 0.0
                    reply = ev.reply
                    session.append(_stored_user if _stored_user is not None else user_text, reply.text)
                    # 这轮干净收尾了,累积器清零——之后到的文字是新话,不是本句的断片,
                    # 别把它拼在已经答完的问题后面。
                    _prev_text = ""
                    # 每轮回写实际模型:主模型失败、语音自动换候选兜底
                    # (voice/session.py _model_candidates)成功后,把实际成功的那个
                    # 记回 chosen_model,下一轮无缝沿用同一模型。但轮中用户若已显式
                    # 切了模型(switch_model 工具写库),此刻 chosen_model ≠ 本轮开跑
                    # 时的 model,再回写会把用户刚切的覆盖掉 —— 只在没人动过时回写。
                    _model_untouched = (
                        session_store.get_chosen_model(session.SESSION_KEY) == model
                    )
                    if reply.model and not reply.is_error and _model_untouched:
                        session_store.set_chosen_model(session.SESSION_KEY, reply.model)
                    # 轮中若切了模型,set_chosen_model 已把 sdk_session_id 清掉(新旧模型
                    # 的 transcript 不兼容,resume 会延续旧模型的调用身份);这里不要再把
                    # 旧 sid 存回去,否则下一轮又 resume 旧 transcript,切换等于白切。
                    if reply.sdk_session_id and _model_untouched:
                        session.set_resume(reply.sdk_session_id)
                    done_payload = {"full_text": reply.text or full_text}
                    if reply.is_error:
                        done_payload["error"] = reply.error or "模型调用失败"
                    await _sse(resp, "done", done_payload)
                    print(
                        f"[voice/send] model={reply.model} first_text={t_first_text - t0:.2f}s "
                        f"first_audio={(t_first_audio - t0) if t_first_audio else -1:.2f}s "
                        f"total={time.monotonic() - t0:.2f}s",
                        flush=True,
                    )
        except (asyncio.CancelledError, ConnectionResetError) as exc:
            # 被新一句抢占(_turn_task.cancel())或客户端断开:把这半轮落库——用户
            # 说过的话不能凭空消失,新一轮的历史里也该看得到"上一句只答了一半"。
            # Claude 侧的停止生成由 stream_turn 自己的取消分支负责(interrupt CLI)。
            print(f"[voice/send] 本轮中止({type(exc).__name__}),已生成 {len(full_text)} 字", flush=True)
            if user_text:
                try:
                    partial = (
                        full_text + " …(话没说完,被下一句打断了)"
                        if full_text.strip()
                        else "(这句还没来得及回答,就被下一句打断了)"
                    )
                    session.append(user_text, partial)
                except Exception:  # noqa: BLE001 —— 落库失败不该在取消路径上再抛
                    pass
        except Exception as exc:  # noqa: BLE001 —— 兜底:出错也要给前端一个 done,不让它干等
            # 2026-07-10 真机教训:这里以前只把错误发给前端就完了,服务端一行不留
            # ——语音场景用户根本不看屏幕,表现就是"发了消息没反应",日志还查无此轮。
            import traceback
            print(f"[voice/send] 本轮异常: {exc!r}\n{traceback.format_exc()}", flush=True)
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
    finally:
        if clarify_token is not None:
            clarify.reset_current(clarify_token)
            clarify.clear_session(session.SESSION_KEY)
        if _turn_task is asyncio.current_task():
            _turn_task = None
            _turn_id = None
        _lock.release()

    # 语音版重启退出:不经过 GatewayRunner._dispatch, 直接在这里检测退出
    if selfops.restart_pending(session.SESSION_KEY):
        selfops.pop_restart_pending(session.SESSION_KEY)

        class _RestartNotifier:
            async def send(self, _chat_id, text: str) -> None:
                await _sse(
                    resp,
                    "system_message",
                    {"text": text, "restarting": text.startswith("♻️")},
                )

        await selfops.exit_for_restart(
            _RestartNotifier(), "voice", session.SESSION_KEY
        )

    return resp


def _emit_sentence(
    queue: "asyncio.Queue", pending: list, seq: int, sentence: str,
    synth: bool = True, filler: bool = False,
) -> int:
    """把这句话的 TTS 合成丢到后台并发跑,不阻塞正在读的 Claude 文字流,见调用处注释。
    synth=False(Omni 出声模式)时只发句子文本不合成音频。
    filler=True 标记这是后端垫的等待话术:前端据此渲染成半透明小气泡(用户反馈
    "念了但文字里看不到"),但仍不算 Claude 回答正文、不落库。"""
    synth_task = None
    if synth:
        synth_task = asyncio.ensure_future(tts.synthesize(sentence, config.VOICE_TTS_VOICE))
        pending.append(synth_task)
    queue.put_nowait((seq, sentence, synth_task, filler))
    return seq + 1


async def _sentence_sender(resp: web.StreamResponse, queue: "asyncio.Queue", first_audio_ts: list) -> None:
    """按 seq 顺序把后台并行合成好的 TTS 音频依次发给客户端——合成并行,发送顺序仍然线性。"""
    while True:
        item = await queue.get()
        if item is None:
            return
        _seq, sentence, synth_task, filler = item
        audio = None
        if synth_task is not None:
            try:
                audio = await synth_task
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 —— 单句合成失败不该拖垮整轮,跳过继续发下一句
                audio = None
        payload = {"text": sentence, "audio_b64": base64.b64encode(audio).decode("ascii") if audio else None}
        if filler:
            payload["filler"] = True
        await _sse(resp, "sentence", payload)
        if not first_audio_ts:
            first_audio_ts.append(time.monotonic())


async def _handle_stop(request: web.Request) -> web.Response:
    if (g := _guard(request)) is not None:
        return g
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    turn_id = (body.get("turn_id") or "").strip()
    if not _TURN_ID_RE.fullmatch(turn_id):
        # 老页面无轮次 ID 时只停掉朗读，不能猜测取消哪一轮模型任务。
        if _stop_event is not None:
            _stop_event.set()
        return web.json_response({"ok": True, "cancelled": False, "reason": "missing_turn_id"})
    cancelled = _cancel_active_turn(turn_id)
    return web.json_response({
        "ok": True,
        "turn_id": turn_id,
        "cancelled": cancelled,
        "reason": None if cancelled else "stale_or_finished",
    })


async def _handle_clear(request: web.Request) -> web.Response:
    """清空语音通话上下文(通话界面顶部的橡皮擦按钮):清的是 voice-chat:main,
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


# ── P1 任务板兼容路由 ───────────────────────────────────────────────────────
# 任务 API 已迁到 gateway.task_routes;这里保留旧 /voice/tasks* 路径,只转换参数后转发,
# 不再维护第二套查询、停止和 SSE 实现。


def _legacy_task_request(request: web.Request) -> web.Request:
    params = dict(request.query)
    params.setdefault("source", "conversation")
    return request.clone(rel_url=request.rel_url.with_query(params))


async def _handle_tasks_list(request: web.Request) -> web.Response:
    return await task_routes.handle_tasks_list(_legacy_task_request(request))


async def _handle_task_detail(request: web.Request) -> web.Response:
    return await task_routes.handle_task_detail(request)


async def _handle_task_stop(request: web.Request) -> web.Response:
    return await task_routes.handle_task_stop(request)


async def _handle_tasks_stream(request: web.Request) -> web.StreamResponse:
    return await task_routes.handle_tasks_stream(request)


def register_routes(app: web.Application) -> None:
    """挂载语音模式全部路由。仅当 config.VOICE_ENABLED 时,web.py 才会调用本函数。"""
    app.add_routes(
        [
            web.get("/voice", _handle_page_gone),
            web.get("/voice/config", _handle_config),
            web.post("/voice/config", _handle_config_post),
            web.post("/voice/stt", _handle_stt),
            web.post("/voice/send", _handle_send),
            web.post("/voice/stop", _handle_stop),
            web.post("/voice/clear", _handle_clear),
            web.post("/voice/debug", _handle_debug),
            web.get("/voice/tasks", _handle_tasks_list),
            web.get("/voice/tasks/stream", _handle_tasks_stream),
            web.get("/voice/tasks/{task_id}", _handle_task_detail),
            web.post("/voice/tasks/{task_id}/stop", _handle_task_stop),
            web.post("/voice/omni/webrtc", _handle_omni_webrtc),
        ]
    )
    # /voice/ws(P2 全双工)已下线:Omni WebRTC 是免提唯一路径,前端 !omniEnabled
    # 时回落按住说话、不再连 WS(见 index.html startHandsFree)。实现本体 ws.py
    # 的删除见 docs/adr/0004。/voice/static/(omni_test.html 联调页)也已随
    # 顶栏扳手入口一起退休(2026-07-12)。
    # F11 重启自愈(task_runner.heal_after_restart)不在这里触发,而在 web.py 的 serve
    # 启动路径里——2026-07-12 事故:测试/脚本只要组建一次 app 就会触发孤儿回收,
    # 把「别的进程里正在跑的任务」误标失败(后台任务收尾跑 pytest 时把自己标死)。
    # register_routes 只做纯粹的路由挂载,不带副作用。
