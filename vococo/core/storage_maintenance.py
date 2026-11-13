"""每日存储养护：只回收明确可重建的缓存，成果只统计、不按年龄删除。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

_DAY = 86400
_MIB = 1024 * 1024
# 这是提醒线，不是删除配额；附件、数据库和恢复备份不能因超限自动丢弃。
_LIMITS = {"附件": 2048 * _MIB, "恢复备份": 1024 * _MIB,
           "数据库": 2048 * _MIB, "日志": 128 * _MIB,
           "提示词缓存": 64 * _MIB, "视频预览": 256 * _MIB, "工作树": 2048 * _MIB}
_PROMPT_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_VIDEO_NAME = re.compile(r"(?:ai_[0-9a-f]{32}|\d+_\d+)\.(?:mp4|mov|m4v|webm|ogv|mkv|avi)\Z")


def _regular(path: Path) -> bool:
    return (path.resolve() == path.absolute() and path.is_file()
            and not path.is_symlink())


def _files(path: Path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("统计目录是软链接或经过软链接")
    if not path.exists():
        return
    if path.is_file():
        yield path
        return
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _files(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


def _measure(paths: list[Path]) -> int:
    # 同一类别内的硬链接只计一次；跨类别的数字不当作实际可释放空间。
    seen, total = set(), 0
    for path in paths:
        for file in _files(path):
            info = file.lstat()
            identity = (info.st_dev, info.st_ino)
            if identity not in seen and stat.S_ISREG(info.st_mode):
                total += info.st_blocks * 512
                seen.add(identity)
    return total


def _cache_source(data: Path, file: Path, kind: str) -> bool:
    if kind == "prompt":
        return bool(_PROMPT_NAME.fullmatch(file.name))
    if not file.name.endswith(".mp4"):
        return False
    name = file.name[:-4]
    return bool(_VIDEO_NAME.fullmatch(name)) and _regular(data / "videos" / name)


def _expire_cache(data: Path, folder: Path, kind: str, now: float) -> dict:
    result = {"removed": 0, "bytes": 0}
    if not folder.exists():
        return result
    if folder.resolve() != folder.absolute() or not folder.is_dir():
        raise ValueError("缓存目录路径异常")
    for file in folder.iterdir():
        if not _regular(file) or not _cache_source(data, file, kind):
            continue
        before = file.lstat()
        if now - before.st_mtime < 30 * _DAY:
            continue
        # 原子替换生成缓存，若检查期间重建了文件，就留给下一次巡检。
        if file.lstat() != before or not _cache_source(data, file, kind):
            continue
        file.unlink()
        result["removed"] += 1
        result["bytes"] += before.st_blocks * 512
    return result


def _database_files(data: Path) -> list[Path]:
    names = ("state.db", "voice/voice.db", "stats.db", "mcp_events.db", "library.db", "research.db")
    return [data / f"{name}{suffix}" for name in names for suffix in ("", "-wal", "-shm")]


def _groups(data: Path, worktrees: list[Path], repositories: Path | None) -> dict[str, list[Path]]:
    # 回收清单只含待处理副本；活跃业务副本也要独立计入，不能等归档后才发现超量。
    copies = [repositories] if repositories is not None else []
    return {"工作树": [data / "worktrees", *worktrees, *copies],
            "附件": [data / name for name in ("audio", "images", "videos", "files")],
            "恢复备份": [data / "private" / "session-recovery"],
            "数据库": _database_files(data),
            "日志": [data / "logs"], "提示词缓存": [data / "prompt_cache"],
            "视频预览": [data / "videos" / "preview"]}


def _previous(path: Path) -> dict:
    if not path.exists():
        return {}
    if not _regular(path):
        raise ValueError("存储回执路径异常")
    previous = json.loads(path.read_text())
    if not isinstance(previous, dict):
        raise ValueError("存储回执损坏")
    return previous


def _write_report(path: Path, report: dict) -> None:
    if path.is_symlink():
        raise ValueError("存储回执路径异常")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def maintain(data: Path, *, worktrees: list[Path] | None = None,
             repositories: Path | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if data.resolve() != data.absolute() or not data.is_dir():
        raise ValueError("数据目录路径异常")
    report_path = data / "storage-maintenance.json"
    previous = _previous(report_path)
    report = {"at": now, "sizes": {}, "limits": _LIMITS, "caches": {}, "errors": []}
    for kind, folder in (("prompt", data / "prompt_cache"), ("preview", data / "videos" / "preview")):
        try:
            report["caches"][kind] = _expire_cache(data, folder, kind, now)
        except (OSError, ValueError):
            report["errors"].append(f"{kind}缓存回收失败；已保留未处理文件")
    for name, paths in _groups(data, worktrees or [], repositories).items():
        try:
            report["sizes"][name] = _measure(paths)
        except (OSError, ValueError):
            report["errors"].append(f"{name}容量检查失败")
    # 保留近一个月的每日采样，避免拿两次紧邻巡检冒充一天的增长。
    samples = [row for row in previous.get("samples", []) if now - 30 * _DAY <= row["at"] <= now]
    if not samples or now - samples[-1]["at"] >= 20 * 3600:
        samples.append({"at": now, "sizes": report["sizes"]})
    report["samples"] = samples[-31:]
    _write_report(report_path, report)
    return report
