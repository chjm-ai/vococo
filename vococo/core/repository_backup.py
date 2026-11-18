"""三十天回收的异盘备份；无法完整验证时不授权删除。"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

from .. import config

ARCHIVE_AGE = 30 * 86400


class BackupUnavailable(ValueError):
    """只传固定原因，不把文件内容或 Git 的原始诊断写入回执。"""


def due(archived_at: float | None, now: float) -> bool:
    return archived_at is not None and math.isfinite(archived_at) and now - archived_at >= ARCHIVE_AGE


@contextmanager
def _destination(path: Path, source: Path):
    if not config.REPOSITORY_BACKUP_DIR:
        raise BackupUnavailable("未配置私有异盘备份目录")
    root = Path(config.REPOSITORY_BACKUP_DIR).expanduser()
    if not root.is_absolute() or root.resolve() != root:
        raise BackupUnavailable("备份目录必须是绝对路径且不经过软链接")
    if any(root.is_relative_to(parent.resolve()) for parent in (
        config.ROOT_DIR, config.DATA_DIR, config.AGENT_WORKSPACES_DIR, source, path,
    )):
        raise BackupUnavailable("备份目录不能位于服务或会话目录内")
    with _directory(root) as descriptor:
        info = os.fstat(descriptor)
        if not _private(info) or not _different_volume(info, path):
            raise BackupUnavailable("备份目录不在另一挂载卷上，或权限不是私有")
        _require_volume_privacy(root)
        yield root, descriptor


def _require_volume_privacy(path: Path) -> None:
    if sys.platform != "darwin":
        return
    output = subprocess.check_output(["/bin/df", "-P", str(path)], timeout=10).splitlines()
    fields = output[1].split() if len(output) == 2 else []
    if not fields or not re.fullmatch(rb"/dev/disk[0-9]+(?:s[0-9]+)*", fields[0]):
        raise BackupUnavailable("无法确认挂载卷强制执行本机权限")
    volume = plistlib.loads(subprocess.check_output(["/usr/sbin/diskutil", "info", "-plist", fields[0].decode()], timeout=10))
    if volume.get("GlobalPermissionsEnabled") is not True:
        raise BackupUnavailable("挂载卷没有启用所有权保护")


@contextmanager
def _directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0


def check_holding(path: Path) -> None:
    _require_volume_privacy(path)
    with _directory(path) as descriptor:
        if not _private(os.fstat(descriptor)):
            raise BackupUnavailable("隔离目录不是私有目录")
    _check_directory_acl(path)


def _different_volume(destination: os.stat_result, path: Path) -> bool:
    return destination.st_dev != path.stat().st_dev


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _check_acl(path: Path) -> None:
    if sys.platform != "darwin":
        return
    result = subprocess.run(["/bin/ls", "-blAeR", str(path)], capture_output=True, timeout=30, check=True)
    if any(line.lstrip().partition(b": ")[0].isdigit() for line in result.stdout.splitlines()):
        raise BackupUnavailable("目录带有访问控制列表，需人工备份")
    _check_directory_acl(path)


def _check_directory_acl(path: Path) -> None:
    if sys.platform != "darwin":
        if hasattr(os, "listxattr") and any(name.startswith("system.posix_acl_")
                                           for name in os.listxattr(path, follow_symlinks=False)):
            raise BackupUnavailable("备份目录带有访问控制列表")
        return
    result = subprocess.run(["/bin/ls", "-blde", str(path)], capture_output=True, timeout=5, check=True)
    if len(result.stdout.splitlines()) != 1:
        raise BackupUnavailable("目录带有访问控制列表，需人工备份")


def _entries(path: Path):
    def raise_error(error):
        raise error

    yield path
    for parent, dirs, files in os.walk(path, followlinks=False, onerror=raise_error):
        if Path(parent) == path and ".git" in files:
            files.remove(".git")
        for name in sorted(dirs + files):
            if name.casefold() == ".git":
                raise BackupUnavailable("包含嵌套 Git 仓库或子模块，需人工备份")
            yield Path(parent) / name


def _stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_flags", 0))


def _file_info(path: Path, device: int) -> os.stat_result:
    info = path.lstat()
    if (info.st_dev != device or not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)) or getattr(info, "st_flags", 0)):
        raise BackupUnavailable("包含跨卷文件、硬链接、文件标记或特殊文件，需人工备份")
    if hasattr(os, "listxattr") and os.listxattr(path, follow_symlinks=False):
        raise BackupUnavailable("存在扩展属性或访问控制列表，需人工备份")
    return info


def _mtime(info: os.stat_result) -> str:
    seconds, nanos = divmod(abs(info.st_mtime_ns), 10**9)
    return f"{'-' if info.st_mtime_ns < 0 else ''}{seconds}.{nanos:09d}"


def _write_member(archive: tarfile.TarFile, path: Path, name: str, info: os.stat_result) -> None:
    member = archive.gettarinfo(str(path), arcname=name)
    member.pax_headers["mtime"] = _mtime(info)
    if not member.isfile():
        archive.addfile(member)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if _stamp(os.fstat(source.fileno())) != _stamp(info):
            raise BackupUnavailable("备份时文件发生变化")
        archive.addfile(member, source)


def _digest(file) -> bytes:
    return hashlib.file_digest(file, "sha256").digest()


def _verify_member(archive: tarfile.TarFile, member: tarfile.TarInfo, path: Path, info: os.stat_result) -> None:
    if (member.mode, member.uid, member.gid, member.pax_headers.get("mtime")) != (
        stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, _mtime(info),
    ):
        raise BackupUnavailable("备份文件权限、所有者或修改时间不一致")
    if member.issym() and stat.S_ISLNK(info.st_mode) and member.linkname == os.readlink(path):
        return
    if member.isdir() and stat.S_ISDIR(info.st_mode):
        return
    if member.isfile() and stat.S_ISREG(info.st_mode):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source, archive.extractfile(member) as saved:
            if (_stamp(os.fstat(source.fileno())) == _stamp(info) and member.size == info.st_size
                    and _digest(source) == _digest(saved)):
                return
    raise BackupUnavailable("备份内容不一致")


def _snapshot(path: Path, destination: Path) -> dict[str, tuple]:
    device = path.stat().st_dev
    stamps = {}
    with destination.open("xb") as output, tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in _entries(path):
            name = entry.relative_to(path).as_posix()
            info = _file_info(entry, device)
            stamps[name] = _stamp(info)
            _write_member(archive, entry, name, info)
    _sync(destination)
    with tarfile.open(destination, "r|") as archive:
        missing = set(stamps)
        for member in archive:
            missing.remove(member.name)
            entry = path / member.name
            _verify_member(archive, member, entry, _file_info(entry, device))
        if missing:
            raise BackupUnavailable("备份遗漏文件")
    return stamps


def _unchanged(path: Path, stamps: dict[str, tuple]) -> bool:
    device = path.stat().st_dev
    return stamps == {entry.relative_to(path).as_posix(): _stamp(_file_info(entry, device)) for entry in _entries(path)}


def _sync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
        if sys.platform == "darwin":
            fcntl.fcntl(handle.fileno(), fcntl.F_FULLFSYNC)


def _git(path: Path, *args: str) -> str:
    from .development_cleanup import _git as git

    return git(path, "--no-lazy-fetch", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", *args)


def _git_revision(path: Path, branch: str, git_dir: Path) -> str:
    from .development_cleanup import _index_metadata_reason, _private_commits, _private_metadata

    if _private_metadata(git_dir) or any(git_dir.glob("*.lock")):
        raise BackupUnavailable("有工作树专属配置或正在进行的 Git 操作")
    if reason := _index_metadata_reason(path, git_dir):
        raise BackupUnavailable(reason)
    if (_git(path, "diff-index", "--cached", "--name-only", "HEAD", "--")
            or any(flag[:1].islower() or flag.startswith("S ") for flag in _git(path, "ls-files", "-v", "-z").split("\0"))
            or any(entry.startswith("160000 ") for entry in _git(path, "ls-files", "--stage", "-z").split("\0"))):
        raise BackupUnavailable("有暂存成果、隐藏索引或子模块，需人工备份")
    head = _git(path, "rev-parse", "HEAD")
    history = _private_commits(path)
    if head != _git(path, "rev-parse", branch) or (history and _git(path, "rev-list", *sorted(history), "--not", head)):
        raise BackupUnavailable("另有未被当前分支包含的历史，需人工备份")
    return head


def _verified_bundle(path: Path, target: Path, branch: str, revision: str) -> None:
    bundle = target / "history.bundle"
    _git(path, "bundle", "create", str(bundle), branch)
    _sync(bundle)
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=target) as scratch:
        folder = Path(scratch)
        _git(folder, "clone", "--no-checkout", "--single-branch", "--branch", branch.removeprefix("refs/heads/"),
             str(bundle), str(folder / "repo"))
        if _git(folder / "repo", "rev-parse", "HEAD") != revision:
            raise BackupUnavailable("备份的 Git 版本不一致")
        _git(folder / "repo", "fsck", "--full", "--no-reflogs")


def _write_json(path: Path, row: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(row, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())


def _receipt(target: Path, row: dict) -> Path:
    directory = config.DATA_DIR.resolve() / "repository-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    receipt = directory / f"{target.name}.json"
    with _directory(directory.parent) as parent, _directory(directory) as descriptor:
        if not _private(os.fstat(descriptor)):
            raise BackupUnavailable("本地备份回执目录不是私有")
        _write_json(receipt, {**row, "backup_path": str(target)})
        os.fsync(descriptor)
        os.fsync(parent)
        _sync(receipt)
    return receipt


@contextmanager
def _publish(root: Path, descriptor: int, prepared: Path, target: Path, row: dict):
    with _directory(prepared) as opened:
        _write_json(prepared / "verified.json", row)
        os.fsync(opened)
        receipt = _receipt(target, row)
        published = False
        try:
            prepared.rename(target)
            published = True
            os.fsync(descriptor)
            _sync(target / "verified.json")
            if not (_same(os.fstat(descriptor), root.lstat()) and _same(os.fstat(opened), target.lstat())):
                raise BackupUnavailable("备份卷或目录已变化")
            yield
        except Exception:
            if published and _same(os.fstat(opened), target.lstat()):
                target.rename(prepared)
                os.fsync(descriptor)
            receipt.unlink(missing_ok=True)
            raise


@contextmanager
def verified_copy(source: Path, path: Path, original: Path, branch: str, git_dir: Path, check):
    from .development_cleanup import _extended_attributes_reason

    with _destination(path, source) as (root, descriptor), tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as scratch:
        _check_directory_acl(root)
        prepared = Path(scratch) / "snapshot"
        prepared.mkdir(mode=0o700)
        revision = _git_revision(path, branch, git_dir)
        _verified_bundle(path, prepared, branch, revision)
        _check_acl(path)
        if reason := _extended_attributes_reason(path):
            raise BackupUnavailable(reason)
        stamps = _snapshot(path, prepared / "files.tar")
        _check_acl(path)
        if reason := _extended_attributes_reason(path) or check(path):
            raise BackupUnavailable(reason)
        if not _unchanged(path, stamps) or _git_revision(path, branch, git_dir) != revision:
            raise BackupUnavailable("备份期间文件或 Git 状态发生变化")
        row = {"original": str(original), "branch": branch, "revision": revision, "verified_at": time.time()}
        target = root / Path(scratch).name.replace(".backup-", "vococo-", 1)
        with _publish(root, descriptor, prepared, target, row):
            if _git_revision(path, branch, git_dir) != revision:
                raise BackupUnavailable("备份期间 Git 状态发生变化")
            if reason := check(path):
                raise BackupUnavailable(reason)
            if not _unchanged(path, stamps):
                raise BackupUnavailable("备份期间文件发生变化")
            if not _same(os.fstat(descriptor), root.lstat()):
                raise BackupUnavailable("备份卷已变化")
        # 到这里才授权删除；删除失败也不撤销已验证备份。
        yield target
