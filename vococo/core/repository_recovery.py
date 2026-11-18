"""为工作树搬移留落盘回执；异常退出后只告警，不盲目移动现场。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from .. import config
from .repository_locks import checkout_retirement_lease


def _directory() -> Path:
    directory = config.DATA_DIR.absolute() / "repository-retirements"
    if directory.resolve() != directory:
        raise ValueError("工作树恢复记录目录经过软链接")
    return directory


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def retirement_record(original: Path, retired: Path):
    directory = _directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = hashlib.sha256(os.fsencode(retired)).hexdigest() + ".json"
    record = directory / filename
    fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"original": str(original), "retired": str(retired)}, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        yield
    finally:
        if not _exists(retired):
            record.unlink(missing_ok=True)


def _read_receipt(record: Path) -> dict | None:
    descriptor = os.open(record, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        row = json.loads(handle.read(32768))
    original, retired = Path(row["original"]), Path(row["retired"])
    holding = retired.parent if retired.name == "checkout" and retired.parent != original.parent else retired
    if not (original.is_absolute() and holding.parent == original.parent
            and holding.name.startswith(f".retiring-{original.name}-")):
        raise ValueError("恢复记录路径不匹配")
    with checkout_retirement_lease(original):
        if not _exists(retired):
            record.unlink(missing_ok=True)
            return None
        return {"path": str(original), "recovery_path": str(retired), "removed": False,
                "reason": "先前回收中断，副本留在恢复路径，请人工核对"}


def pending_retirements() -> list[dict]:
    results = []
    for record in _directory().glob("*.json"):
        try:
            if row := _read_receipt(record):
                results.append(row)
        except (BlockingIOError, FileNotFoundError):
            continue
        except (OSError, ValueError, TypeError, KeyError):
            results.append({"path": str(record), "removed": False, "reason": "工作树恢复记录无法读取，请人工核对"})
    return results
