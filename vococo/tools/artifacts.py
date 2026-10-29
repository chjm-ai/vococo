"""私有文件成果快照；与 HTML 预览共用文件读取及会话附件存储。"""
from __future__ import annotations

import asyncio
import io
import lzma
import stat
import tarfile
import zipfile
import zlib
from pathlib import Path

from . import danger
from .file_snapshot import check_parts as _check_parts, read_regular

MAX_BYTES = 64 * 1024 * 1024
ARCHIVES = {".zip", ".pptx", ".docx", ".xlsx"}
KEY_CONTAINERS = {".key", ".pem", ".der", ".p12", ".pfx", ".p8", ".jks", ".keystore", ".kdbx"}
CODE_EXTENSIONS = {".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".zsh",
                   ".go", ".rs", ".c", ".h", ".cpp", ".java", ".rb", ".php",
                   ".swift", ".kt", ".ps1", ".sql", ".lua", ".vue", ".svelte",
                   ".html", ".htm", ".css", ".cs", ".m", ".mm", ".r", ".pl",
                   ".bat", ".cmd", ".psm1", ".psd1", ".vbs", ".vb", ".fish"}


def _delivery_links(session_key: str) -> tuple[Path, ...]:
    from .. import config
    from ..core.execution import linked_file_roots

    safe = []
    for root in linked_file_roots(session_key):
        # 运行状态、Agent 配置及其他会话不能因一次关联授权变成下载源。
        if root.is_relative_to(config.DATA_DIR.resolve()) or Path.home().resolve().is_relative_to(root):
            continue
        try:
            _check_parts(root.parts[1:])
        except ValueError:
            continue
        safe.append(root)
    return tuple(safe)


def _roots(session_key: str, cwd: str) -> tuple[Path, ...]:
    from ..core.execution import policy_for_session

    policy = policy_for_session(session_key, cwd)
    roots = (policy.workspace, policy.output_dir) if policy else (Path(cwd),)
    roots = tuple(dict.fromkeys((*roots, *_delivery_links(session_key))))
    safe = []
    for root in roots:
        root = root.expanduser().resolve()
        # HOME 及其祖先不是项目；即使错误配置也不能变成任意文件下载。
        if Path.home().resolve().is_relative_to(root) or root == Path(root.anchor):
            raise ValueError("工作目录过宽，不能安全交付文件。")
        parts = root.parts[1:]
        # SDK 开发 worktree 位于 .claude/worktrees；这不是读取 .claude 凭据的授权。
        _check_parts(tuple(part for i, part in enumerate(parts)
                           if not (part == ".claude" and parts[i + 1:i + 2] == ("worktrees",))))
        safe.append(root)
    return tuple(safe)


def _read_source(root: Path, parts: tuple[str, ...]) -> bytes:
    return read_regular(root, parts, MAX_BYTES)


def _check_key_container(filename: str) -> None:
    if Path(filename).suffix.lower() in KEY_CONTAINERS:
        raise ValueError("不接受可能包含私钥的密钥容器。")


def _looks_der(data: bytes) -> bool:
    if len(data) < 2 or data[0] != 0x30:
        return False
    size = data[1]
    if size < 128:
        return size + 2 == len(data)
    width = size & 0x7f
    if not 1 <= width <= 4 or len(data) < 2 + width:
        return False
    return int.from_bytes(data[2:2 + width], "big") + 2 + width == len(data)


def _check_secrets(data: bytes) -> None:
    # 无法安全区分 DER 私钥和证书，也不能可靠扫描数据库内的敏感字段。
    if _looks_der(data) or data.startswith(b"SQLite format 3\x00"):
        raise ValueError("不接受可能含凭据的 DER 容器或数据库。")
    texts = [data.decode("utf-8", errors="ignore")]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:4096]:
        texts.extend(data.decode(encoding, errors="ignore") for encoding in ("utf-16-le", "utf-16-be"))
    for text in texts:
        if danger.redact_secrets(text) != text or "-----BEGIN " in text and "PRIVATE KEY-----" in text:
            raise ValueError("成果含疑似密钥，拒绝交付。")


def _is_zip(data: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(data))


def _is_tar(data: bytes) -> bool:
    if len(data) < 512:
        return False
    try:
        tarfile.TarInfo.frombuf(data[:512], "utf-8", "surrogateescape")
        return True
    except tarfile.HeaderError:
        return False


def _check_unsupported_archive(filename: str, data: bytes) -> None:
    suffixes = {".gz", ".bz2", ".xz", ".7z", ".rar", ".zst", ".tar", ".tgz", ".lz4"}
    signatures = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c",
                  b"Rar!\x1a\x07", b"\x28\xb5\x2f\xfd", b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18")
    lz4_skippable = len(data) >= 4 and 0x184D2A50 <= int.from_bytes(data[:4], "little") <= 0x184D2A5F
    if Path(filename).suffix.lower() in suffixes or data.startswith(signatures) or lz4_skippable or _is_tar(data):
        raise ValueError("此压缩格式暂无法安全检查，请使用 ZIP。")


def _archive_parts(entry: zipfile.ZipInfo) -> None:
    parts = Path(entry.filename).parts
    # OOXML 的标准关系文件 _rels/.rels 不是隐藏凭据；其他隐藏项仍拒绝。
    checked = (*parts[:-1], "relationships.rels") if parts and parts[-1] == ".rels" else parts
    _check_parts(checked)
    if not entry.is_dir():
        _check_key_container(entry.filename)
    if entry.filename.startswith("/") or ".." in parts or "\\" in entry.filename:
        raise ValueError("压缩包含越界路径。")
    if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
        raise ValueError("不接受软链或加密压缩包。")


def _check_archive(data: bytes, depth: int = 0, budget: list[int] | None = None,
                   code_paths: list[str] | None = None, prefix: str = "") -> None:
    budget = budget if budget is not None else [0, 0]
    if depth > 2:
        raise ValueError("压缩嵌套过深，无法安全检查。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            budget[0] += len(entries)
            budget[1] += sum(e.file_size for e in entries)
            if budget[0] > 5000 or budget[1] > MAX_BYTES * 2:
                raise ValueError("压缩内容过大，无法安全检查。")
            for entry in entries:
                _archive_parts(entry)
                if entry.is_dir():
                    continue
                payload = archive.read(entry)
                _check_secrets(payload)
                name = f"{prefix}{entry.filename}"
                if code_paths is not None and (Path(entry.filename).suffix.lower() in CODE_EXTENSIONS or payload.startswith(b"#!")):
                    code_paths.append(name)
                _check_unsupported_archive(entry.filename, payload)
                # PPT 图表嵌入 XLSX 是正常成果，同样递归检查并共用解压预算。
                if _is_zip(payload) or payload.startswith(b"PK\x03\x04"):
                    _check_archive(payload, depth + 1, budget, code_paths, name + " → ")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, lzma.LZMAError, EOFError) as exc:
        raise ValueError("压缩包无法安全检查。") from exc


def _snapshot(path: str, session_key: str, cwd: str) -> tuple[str, bytes]:
    if not isinstance(path, str) or not path.strip() or "://" in path or "\x00" in path or "\\" in path:
        raise ValueError("path 必须是本地成果文件路径。")
    candidate = Path(path).expanduser()
    candidate = candidate if candidate.is_absolute() else Path(cwd).resolve() / candidate
    if ".." in candidate.parts:
        raise ValueError("不接受目录逃逸。")
    from .. import config

    runtime = config.DATA_DIR.resolve()
    for root in _roots(session_key, cwd):
        if candidate.is_relative_to(root):
            parts = candidate.relative_to(root).parts
            _check_parts(parts)
            _check_key_container(candidate.name)
            if candidate.is_relative_to(runtime) and not root.is_relative_to(runtime):
                raise ValueError("运行数据目录不是当前会话的成果来源。")
            data = _read_source(root, parts)
            _check_secrets(data)
            _check_unsupported_archive(candidate.name, data)
            if candidate.suffix.lower() in ARCHIVES or data.startswith(b"PK\x03\x04") or _is_zip(data):
                _check_archive(data)
            return candidate.name, data
    raise ValueError("文件不在当前会话工作目录或正式成果目录内，未读取。")


def code_files(filename: str, data: bytes) -> list[str]:
    """识别常见源代码供模型审核；不能保证识别所有格式。"""
    paths = []
    if Path(filename).suffix.lower() in CODE_EXTENSIONS or data.startswith(b"#!"):
        paths.append(filename)
    if Path(filename).suffix.lower() in ARCHIVES or data.startswith(b"PK\x03\x04") or _is_zip(data):
        _check_archive(data, code_paths=paths)
    return paths


async def snapshot(path: str, session_key: str) -> tuple[str, bytes]:
    cwd = danger.current_cwd()
    if not cwd:
        raise ValueError("当前会话没有工作目录，无法安全交付。")
    return await asyncio.to_thread(_snapshot, path, session_key, cwd)
