"""语音转文字:主界面语音输入(gateway/adapters/web.py 的 /transcribe)和
语音伴聊模式共用同一套阿里 DashScope 转写实现,避免两边各写一份、切供应商时忘了同步
(2026-07-08 切阿里云时就出过这个问题:web.py 曾经留着一份没跟着切的旧 SenseVoice
实现,属性名对不上导致每次转写都报错)。
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .. import config

_DASHSCOPE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

# 与 web.py 的 _STT_CLEANUP_PROMPT 同一份措辞,清洗逐字稿口癖/同音字/音译错的专名,
# 额外加了 Obsidian 这个语音场景实测识别率差的例子(笔记软件,常被听成不相关的中文谐音)
_CLEANUP_PROMPT = (
    "你是语音转文字校对员。下面是一段语音识别生成的逐字稿,请只做这几件事:"
    "删掉「呃、啊、嗯、然后、就是、那个」这类无意义口头禅;"
    "修正同音字/错别字;修正被听写成中文谐音的英文专有名词"
    "(常见于科技/AI 话题,比如把 OpenAI、Codex、Anthropic、Claude Code、Obsidian 这类词"
    "听成谐音汉字或走音英文,要按读音猜回正确的英文原词,保留其标准大小写拼写;"
    "这类专名不一定是本例举出的,只要读音明显是某个科技/软件专有名词的谐音就应该按此规则纠正);"
    "补全必要标点。"
    "若文本以 [说话人N] 开头的分段形式存在(会议转写的说话人标记),必须原样保留"
    "这些前缀,不得删除或改写。"
    "禁止:不要改写句子结构,不要删减或添加信息,不要翻译。"
    "只输出清洗后的文本,不要加引号,不要任何解释。"
)


async def read_audio(request: web.Request) -> tuple[bytes | None, str, str]:
    """从 multipart 里取出 audio 字段(字节、文件名、类型)。"""
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return None, "", ""
    async for part in reader:
        if part.name == "audio":
            data = await part.read(decode=False)
            ctype = part.headers.get("Content-Type", "application/octet-stream")
            return data, (part.filename or "voice.webm"), ctype
    return None, "", ""


async def transcribe(
    audio: bytes, filename: str, ctype: str, *, timeout_sec: int = 30
) -> tuple[str | None, str]:
    """转写音频,返回 (text, error)。text 为 None 表示失败,error 是给用户看的提示。

    识别本体是阿里 DashScope 的 qwen3-asr-flash(2026-07-08 从 SiliconFlow 的
    SenseVoiceSmall 切过来,真机实测 SenseVoice 单次识别要 8~17 秒,隔离测试
    确认是那个接口本身慢、不是我们代码的问题;qwen3-asr-flash 同等准确度下
    只要 0.5~1 秒,见 03-phase2-实现记录.md)。协议跟 SiliconFlow 完全不同——
    这边是 JSON + base64 音频(data URI),不是 multipart 文件上传。

    timeout_sec:语音输入(几秒到几十秒的口述)默认 30s 足够;聊天里上传的音频
    附件可能长达数十分钟、上百 MB,调用方(web.py 的 /upload_audio)会传更大的值。
    """
    if not config.DASHSCOPE_API_KEY:
        return None, "未配置语音转写:请在 .env 设 DASHSCOPE_API_KEY"
    mime = (ctype or "audio/wav").split(";")[0].strip() or "audio/wav"
    b64 = base64.b64encode(audio).decode("ascii")
    payload = {
        "model": config.DASHSCOPE_STT_MODEL,
        "input": {
            "messages": [
                {"role": "user", "content": [{"audio": f"data:{mime};base64,{b64}"}]}
            ]
        },
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(_DASHSCOPE_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
        t1 = time.monotonic()
        if resp.status != 200:
            # 400 细分:探测失败的超长音频兜底提示(正常路径已按时长分流,到不了这)
            if resp.status == 400 and "too long" in body:
                return None, "音频超过转写服务时长限制,暂不支持"
            return None, f"转写服务返回 {resp.status}"
        choices = json.loads(body).get("output", {}).get("choices") or []
        parts = choices[0]["message"]["content"] if choices else []
        text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
        cleaned = await _cleanup(text)
        t2 = time.monotonic()
        print(
            f"[voice/stt] size={len(audio)/1024:.1f}KB stt={t1-t0:.2f}s clean={t2-t1:.2f}s\n"
            f"  raw:     {text!r}\n"
            f"  cleaned: {cleaned!r}",
            flush=True,
        )
        return cleaned, ""
    except (aiohttp.ClientError, TimeoutError):
        return None, "转写服务连接失败"
    except (json.JSONDecodeError, ValueError, KeyError, IndexError):
        return None, "转写返回解析失败"


async def _cleanup(text: str) -> str:
    """清洗口癖/同音字;关闭或失败都原样返回未清洗文本,不阻塞主流程。"""
    if not text or not config.STT_CLEANUP_ENABLED or not config.STT_API_KEY:
        return text
    url = f"{config.STT_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.STT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.STT_CLEANUP_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _CLEANUP_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
        if resp.status != 200:
            return text
        content = json.loads(body)["choices"][0]["message"]["content"]
        return content.strip() or text
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return text


# ── 会议录音(≥ 40 分钟):paraformer 异步文件转写 + 说话人分离 ────────────────
# paraformer 与 qwen3-asr-flash 不同:它是"提交任务→轮询"的异步接口,且 input 只
# 收公网可下载 URL(阿里侧拉取),不收 base64。所以先把音频临时放进 PUBLISHED_DIR
# (经 /pub 路由公网暴露,随机文件名防枚举),转写完立刻删,残留也定期清。
_DASHSCOPE_ASR_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
_DASHSCOPE_TASKS_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
_MEETING_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".amr", ".mp4", ".mov", ".webm"}


def _safe_suffix(filename: str) -> str:
    """从文件名取受支持的后缀,怪后缀一律 .bin(paraformer 按内容嗅探格式)。"""
    s = Path(filename or "").suffix.lower()
    return s if s in _MEETING_SUFFIXES else ".bin"


def _meeting_tmp_dir() -> Path:
    d = config.AUDIO_DIR / ".meeting"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup_stale_meeting_files() -> None:
    """清掉进程崩溃残留的会议临时文件:公网 /pub 下暴露超 1 小时的 mt* 一律删。"""
    cutoff = time.time() - 3600
    for d in (config.PUBLISHED_DIR, _meeting_tmp_dir()):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file() and f.name.startswith(("mt", "probe", "mono")):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass


async def probe_duration(audio: bytes, filename: str) -> float | None:
    """ffprobe 探测音频时长(秒);ffmpeg 缺失或格式太怪返回 None(调用方当短录音处理)。"""
    suffix = _safe_suffix(filename)
    p = _meeting_tmp_dir() / f"probe{secrets.token_hex(4)}{suffix}"
    try:
        await asyncio.to_thread(p.write_bytes, audio)
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(p),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        info = json.loads(out or b"{}")
        try:
            d = float(info.get("format", {}).get("duration", 0) or 0)
            return d if d > 0 else None
        except (TypeError, ValueError):
            return None
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


async def _meeting_channels(p: Path) -> int | None:
    """探测声道数(说话人分离只支持单声道,>1 需先转码)。"""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(p),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        for s in json.loads(out or b"{}").get("streams") or []:
            if s.get("codec_type") == "audio":
                return int(s.get("channels", 0) or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


async def _to_mono_wav(src: Path, dst: Path) -> bool:
    """ffmpeg 转 16kHz 单声道 wav(paraformer 说话人分离的硬要求,顺便压缩体积)。"""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"[voice/stt] 转单声道失败:{err.decode(errors='ignore')[:200]}", flush=True)
        return False
    return True


async def _format_meeting(transcripts: list[dict]) -> str:
    """把 paraformer 结果 JSON 的 transcripts(音轨数组)聚合成 [说话人N] 分段。

    说话人分离输出在 sentence 级(speaker_id 字段);编号按首次开口顺序排,
    连续同一人的句段合并成一段。分离没生效(无 speaker_id)则退回整段全文。
    """
    tr = (transcripts or [{}])[0]  # 多音轨取第一个(默认只转第一个音轨)
    sentences = tr.get("sentences") or []
    spk_ids = [
        s.get("speaker_id") for s in sentences
        if (s.get("text") or "").strip() and s.get("speaker_id") is not None
    ]
    if not spk_ids:
        return await _cleanup((tr.get("text") or "").strip())
    labels = {sid: i + 1 for i, sid in enumerate(dict.fromkeys(spk_ids))}
    lines: list[str] = []
    cur_spk, cur_text = None, []
    for s in sentences:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        spk = s.get("speaker_id")
        if spk != cur_spk:
            if cur_spk is not None and cur_text:
                lines.append(f"[说话人{labels[cur_spk]}] {''.join(cur_text)}")
                cur_text = []
            cur_spk = spk
        if spk in labels:
            cur_text.append(text)
        elif cur_text:  # 个别句没带 speaker_id,并入上一段
            cur_text[-1] += text
    if cur_text and cur_spk in labels:
        lines.append(f"[说话人{labels[cur_spk]}] {''.join(cur_text)}")
    return await _cleanup("\n".join(lines))


async def transcribe_meeting(
    audio: bytes,
    filename: str,
    ctype: str,
    *,
    host: str,
    diarize: bool = True,
    total_sec: int = 900,
) -> tuple[str | None, str]:
    """长音频转写:阿里 paraformer-v2 异步文件转写,可选说话人分离。

    qwen3-asr-flash 有 5 分钟时长上限(实测 400),更长的音频(由 transcribe_attachment
    分流)走 paraformer 异步接口:临时把音频放进 PUBLISHED_DIR 走 /pub 公网路由
    (paraformer 只收可下载 URL,不收 base64),随机文件名+转写完即删。任一环节
    失败都降级回 transcribe()(至少出全文),不把失败当结果抛给用户。

    diarize: 会议录音(≥ 40 分钟)要区分说话人,输出带 [说话人N] 分段;个人长
    录音(4.5~40 分钟)关掉,避免转写文本被前缀污染。
    host: 本机服务的公网 Host(经 cloudflared 穿透),临时 URL 按它拼。
    total_sec: 提交+轮询的总预算,40 分钟以上音频转写+分离通常几分钟内完成。
    """
    if not config.DASHSCOPE_API_KEY:
        return None, "未配置语音转写:请在 .env 设 DASHSCOPE_API_KEY"
    _cleanup_stale_meeting_files()
    pub_file: Path | None = None
    try:
        # 1. 写临时文件 + 探测声道;多声道先转 16k 单声道 wav(分离的硬要求)
        suffix = _safe_suffix(filename)
        src = _meeting_tmp_dir() / f"probe{secrets.token_hex(4)}{suffix}"
        await asyncio.to_thread(src.write_bytes, audio)
        channels = await _meeting_channels(src)
        if channels is not None and channels > 1:
            mono = _meeting_tmp_dir() / f"mono{secrets.token_hex(4)}.wav"
            if await _to_mono_wav(src, mono):
                src = mono
                suffix = ".wav"
        # 2. 挪进 /pub 公网目录(随机名),拼出阿里可下载的 URL
        config.PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
        pub_file = config.PUBLISHED_DIR / f"mt{secrets.token_hex(8)}{suffix}"
        src.rename(pub_file)
        url = f"https://{host}/pub/{pub_file.name}"
        # 3. 提交异步转写任务(说话人分离+去口癖+中英),再轮询到出结果
        params: dict = {
            "channel_id": [0],
            "language_hints": ["zh", "en"],
            "disfluency_removal_enabled": True,
        }
        if diarize:
            params["diarization_enabled"] = True
        payload = {
            "model": config.DASHSCOPE_MEETING_MODEL,
            "input": {"file_urls": [url]},
            "parameters": params,
        }
        headers = {
            "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            async with sess.post(_DASHSCOPE_ASR_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"提交任务返回 {resp.status}")
            task_id = json.loads(body)["output"]["task_id"]
            deadline = time.monotonic() + total_sec
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                async with sess.get(
                    _DASHSCOPE_TASKS_URL.format(task_id=task_id), headers=headers
                ) as resp:
                    body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"查询任务返回 {resp.status}")
                data = json.loads(body)
                status = (data.get("output") or {}).get("task_status")
                if status == "SUCCEEDED":
                    # 转写正文不在轮询响应里,要再下载结果 OSS 的签名 URL
                    results = (data.get("output") or {}).get("results") or []
                    trans_url = None
                    for r in results:
                        u = r.get("transcription_url")
                        if not u and r.get("results"):
                            u = r["results"][0].get("transcription_url") if r["results"] else None
                        if u:
                            trans_url = u
                            break
                    if not trans_url:
                        raise RuntimeError("结果没有 transcription_url")
                    async with sess.get(trans_url) as resp:
                        body = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"下载转写结果返回 {resp.status}")
                    transcripts = (json.loads(body) or {}).get("transcripts") or []
                    text = await _format_meeting(transcripts)
                    if text:
                        print(
                            f"[voice/stt] 会议转写完成 size={len(audio)/1024/1024:.1f}MB "
                            f"分段数={text.count(chr(10)) + 1}",
                            flush=True,
                        )
                        return text, ""
                    raise RuntimeError("转写结果为空")
                if status == "FAILED":
                    raise RuntimeError("转写任务失败")
            raise TimeoutError("会议转写超时")
    except Exception as e:  # noqa: BLE001——降级是设计内路径,任何失败都回退
        print(f"[voice/stt] 会议转写失败({e}),降级 qwen3-asr-flash", flush=True)
        return await transcribe(audio, filename, ctype, timeout_sec=180)
    finally:
        if pub_file is not None:
            try:
                pub_file.unlink(missing_ok=True)
            except OSError:
                pass


async def transcribe_attachment(
    audio: bytes, filename: str, ctype: str, *, host: str, timeout_sec: int = 180
) -> tuple[str | None, str]:
    """附件转写入口(web.py /upload_audio 用):ffprobe 探测时长三档分流——
    < 4.5 分钟 → qwen3-asr-flash(快,它有 5 分钟时长上限);
    4.5~40 分钟 → paraformer 异步转写,不开说话人分离(个人长录音,如手机录音);
    ≥ 40 分钟 → paraformer + 说话人分离(会议)。
    探测失败(无 ffmpeg/格式太怪)当短录音处理,不影响上传。"""
    if config.DASHSCOPE_API_KEY and audio:
        duration = await probe_duration(audio, filename)
        if duration is not None and duration >= config.MEETING_ASR_MIN_SECONDS:
            return await transcribe_meeting(audio, filename, ctype, host=host, diarize=True)
        if duration is not None and duration >= config.ASR_FLASH_MAX_SECONDS:
            return await transcribe_meeting(audio, filename, ctype, host=host, diarize=False)
    return await transcribe(audio, filename, ctype, timeout_sec=timeout_sec)
