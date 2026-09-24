"""视频落盘(Web 消息)。

视频本体写进 config.VIDEOS_DIR,连同原始文件名和 MIME 记进 turns.videos(JSON 列表,
元素 {"file":落盘名,"filename":原始文件名,"media_type":MIME})。

跟 images.py 的关键区别:图片能直接当多模态 content block 喂给模型,视频【不能】——
Claude Messages 协议没有 video block(这层协议各供应商一致),硬塞进去只会被拒或把
几十 MB base64 打进请求体。所以视频走的是音频那条路:落盘 + 把本机路径告诉模型,
要"看懂"内容由模型自己用 ffmpeg 抽帧/抽音轨再处理。

命名沿用 images.py 的约定:AI 主动发的视频(send_video 工具)用 "ai_" 前缀,用户上传的
是 "{turn_id}_{idx}.ext"(数字开头),靠前缀就能把两类拆开分别贴回 AI 气泡和用户气泡。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from .. import config
from . import _db

_VIDEO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # 文件名白名单,挡路径穿越

AI_VIDEO_PREFIX = "ai_"

# 前端/工具两侧共用的一份格式白名单:浏览器 <video> 真能播的那几种容器。
# mkv/avi 之类落盘没问题但网页播不了,发出去就是个黑框,不如直接拒并提示转码。
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "ogv", "ogg"}


def _video_ext(media_type: str, filename: str = "") -> str:
    """取一个安全的扩展名:原始文件名优先,其次 media_type,都取不到回落 mp4。

    优先文件名的理由同音频:浏览器给 mov/m4v 的 type 常常为空或不准,落成
    .quicktime 这类扩展名会让 <video> 和 ffmpeg 都认不出容器。
    """
    from_name = (filename or "").rsplit(".", 1)[-1] if "." in (filename or "") else ""
    from_name = re.sub(r"[^a-z0-9]", "", from_name.strip().lower())
    if from_name and len(from_name) <= 5:
        return from_name
    ext = (media_type or "").split("/")[-1].split(";")[0].strip().lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)
    return ext or "mp4"


def save_turn_videos(turn_id: int, videos: list) -> list[dict]:
    """把某轮用户视频写盘并把 {file,filename,media_type} 记进 turns.videos;返回写入的列表。

    videos 元素需有 .data(原始字节,来自 multipart 上传,不是 base64)、.media_type、
    .filename。同时把落盘路径回填到 .local_path —— 模型要抽帧/剪辑/转码就靠它。
    """
    if not videos:
        return []
    config.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for idx, vi in enumerate(videos):
        data = getattr(vi, "data", None)
        if not data:
            continue
        filename = str(getattr(vi, "filename", "") or "")
        media_type = str(getattr(vi, "media_type", "") or "")
        name = f"{turn_id}_{idx}.{_video_ext(media_type, filename)}"
        path = config.VIDEOS_DIR / name
        path.write_bytes(data)
        try:
            vi.local_path = str(path)
        except AttributeError:
            pass
        entries.append({"file": name, "filename": filename, "media_type": media_type})
    if entries:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET videos=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), turn_id),
        )
        c.commit()
    return entries


def clone_turn_videos(turn_id: int, entries: list) -> list[dict]:
    """把一批已落盘的视频复制成属于 turn_id 的新副本,返回新的 entries。

    同 images/audio:副本会话要持有自己的文件,否则删原会话会把副本的视频一起清掉
    (purge 按文件名删)。源文件缺失的条目整条丢弃——视频只有文件本身有意义,
    不像音频还留得下转写文字。
    """
    out: list[dict] = []
    config.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        old = e.get("file") or ""
        if not _VIDEO_NAME_RE.match(old):
            continue
        src = config.VIDEOS_DIR / old
        if not src.is_file():
            continue
        ext = old.rsplit(".", 1)[-1] if "." in old else "mp4"
        # AI 发的视频必须保住 ai_ 前缀:历史渲染靠它区分贴用户气泡还是 AI 气泡
        prefix = AI_VIDEO_PREFIX if old.startswith(AI_VIDEO_PREFIX) else ""
        new = f"{prefix}{turn_id}_{idx}.{ext}"
        (config.VIDEOS_DIR / new).write_bytes(src.read_bytes())
        new_entry = dict(e)
        new_entry["file"] = new
        out.append(new_entry)
    return out


def append_turn_video(session_key: str, entry: dict) -> None:
    """AI 主动发的一个视频(send_video 工具)追加进当前(最新)一轮的 turns.videos。

    与 save_turn_videos 的区别同 images.append_turn_image:那是用户上传时整轮一次性
    写入,这里是轮次进行中途补发,必须在已有列表上【追加】,否则会把这一轮用户
    上传的视频记录冲掉。找不到轮次则静默跳过。
    """
    c = _db.conn()
    row = c.execute(
        "SELECT id, videos FROM turns WHERE session_key=? ORDER BY id DESC LIMIT 1",
        (session_key,),
    ).fetchone()
    if not row:
        return
    turn_id, vids = row
    try:
        entries = json.loads(vids) if vids else []
    except (json.JSONDecodeError, ValueError):
        entries = []
    entries.append(entry)
    c.execute(
        "UPDATE turns SET videos=? WHERE id=?",
        (json.dumps(entries, ensure_ascii=False), turn_id),
    )
    c.commit()


def probe_meta(path) -> str:
    """尽力探一份视频元信息摘要(时长/分辨率/体积),给模型和用户看;探不到只给体积。

    没装 ffmpeg 或文件损坏时不报错、不阻断发送 —— 元信息是锦上添花,缺了也只是
    模型少知道一点,不该让整条消息发不出去。
    """
    import shutil
    import subprocess

    parts: list[str] = []
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "format=duration:stream=width,height",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=5,
            ).stdout.split()
            if len(out) >= 3:
                w, h, dur = out[0], out[1], float(out[2])
                parts.append(f"{int(dur // 60)}分{int(dur % 60)}秒")
                parts.append(f"{w}x{h}")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        parts.append(f"{path.stat().st_size / 1024 / 1024:.1f}MB")
    except OSError:
        pass
    return " · ".join(parts)


def video_path(name: str):
    """按文件名返回视频的磁盘路径(Path);非法名/不存在返回 None —— 供 HTTP 取流时校验。"""
    if not name or not _VIDEO_NAME_RE.match(name):
        return None  # 挡 ../ 等路径穿越
    p = config.VIDEOS_DIR / name
    return p if p.is_file() else None


# ── 预览版(低码率副本)──────────────────────────────────────────────────
# 聊天气泡里播放走预览版,下载才拉原片:跨境隧道带宽很低,几十 MB 的原片边播边缓冲
# 会卡很久。预览版限长边 960、码率封顶 700k,实测 95 秒竖屏 43MB → 2.3MB、转码 17 秒。
# 存在 VIDEOS_DIR/preview/<原名>.mp4,不跟原片混放(原片目录的命名/前缀规则不受影响)。
PREVIEW_MIN_BYTES = 4 * 1024 * 1024  # 原片比这还小就直接播原片,转码不划算
_PREVIEW_TIMEOUT = 600
_preview_jobs: set[str] = set()     # 正在转的,防同一视频被并发请求重复开 ffmpeg
_preview_failed: set[str] = set()   # 转失败的,本进程内不再重试(否则每次播放都白跑一遍)
_preview_sem: asyncio.Semaphore | None = None  # 同一时刻只转一个,别把机器 CPU 吃满


def _preview_file(name: str) -> Path:
    return config.VIDEOS_DIR / "preview" / f"{name}.mp4"


def preview_path(name: str) -> Path | None:
    """已生成好的预览版路径;没有(未生成/转码中/原片太小/非法名)返回 None。"""
    if not name or not _VIDEO_NAME_RE.match(name):
        return None
    p = _preview_file(name)
    return p if p.is_file() else None


def needs_preview(name: str) -> bool:
    """这个视频值不值得、还需不需要去生成预览版。"""
    src = video_path(name)
    if src is None or name in _preview_jobs or name in _preview_failed:
        return False
    if _preview_file(name).is_file() or not shutil.which("ffmpeg"):
        return False
    try:
        return src.stat().st_size >= PREVIEW_MIN_BYTES
    except OSError:
        return False


async def ensure_preview(name: str) -> None:
    """后台生成预览版;已有/在转/不值得转时直接返回。失败只记一笔,不抛错。"""
    global _preview_sem
    if not needs_preview(name):
        return
    _preview_jobs.add(name)
    dst = _preview_file(name)
    tmp = dst.with_name(f"{name}.tmp.mp4")  # 先写临时文件再改名:没转完的半截文件绝不能被拿去播
    try:
        if _preview_sem is None:
            _preview_sem = asyncio.Semaphore(1)
        async with _preview_sem:
            dst.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y",
                "-i", str(config.VIDEOS_DIR / name),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale=w='min(960,iw)':h='min(960,ih)'"
                       ":force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-maxrate", "700k", "-bufsize", "1400k", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "64k", "-ac", "2",
                "-movflags", "+faststart",  # moov 放文件头,手机不用等下完就能起播
                str(tmp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(proc.wait(), _PREVIEW_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                code = -1
        if code == 0 and tmp.is_file():
            os.replace(tmp, dst)
        else:
            _preview_failed.add(name)
    except OSError:
        _preview_failed.add(name)
    finally:
        tmp.unlink(missing_ok=True)
        _preview_jobs.discard(name)


def purge_session_videos(c: sqlite3.Connection, session_key: str) -> None:
    """删会话前把它名下所有视频文件从磁盘清掉,避免孤儿文件堆积(视频体积大,更不能留)。"""
    rows = c.execute(
        "SELECT videos FROM turns WHERE session_key=? AND videos IS NOT NULL",
        (session_key,),
    ).fetchall()
    for (vids,) in rows:
        try:
            entries = json.loads(vids) if vids else []
        except (json.JSONDecodeError, ValueError):
            continue
        for e in entries:
            name = (e.get("file") or "") if isinstance(e, dict) else ""
            if _VIDEO_NAME_RE.match(name or ""):
                (config.VIDEOS_DIR / name).unlink(missing_ok=True)
                _preview_file(name).unlink(missing_ok=True)
