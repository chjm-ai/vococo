"""作品索引与独立原件；不依赖聊天附件的生命周期。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
from urllib.parse import urlsplit
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from .. import config
from . import agents

IMAGE_MAX_BYTES = 20 * 1024 * 1024
THUMBNAIL_SIZE = 480
_THUMBNAIL_PARAMETERS = f"webp:{THUMBNAIL_SIZE}:quality=80:exif-transpose:first-frame:v1"
_THUMBNAIL_LOCKS = [threading.Lock() for _ in range(32)]


class ThumbnailVersionError(ValueError):
    """请求版本已过期，客户端需重新读取作品列表。"""

_IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                  "GIF": ("gif", "image/gif"), "WEBP": ("webp", "image/webp")}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_items (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
 storage_key TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
 hidden INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library_sources (
 id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES library_items(id),
 action TEXT NOT NULL, session_key TEXT NOT NULL, turn_id INTEGER,
 metadata TEXT NOT NULL, event_key TEXT UNIQUE, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS library_sources_item ON library_sources(item_id);
"""


@contextmanager
def _connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DATA_DIR / "library.db", timeout=15)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


def validate_image(data: bytes) -> tuple[str, str]:
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise ValueError("图片为空或超过 20MB 上限")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _IMAGE_FORMATS:
                raise ValueError("仅支持 PNG、JPEG、GIF、WebP 图片")
            if image.width * image.height > 40_000_000:
                raise ValueError("图片像素数超过上限")
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片内容无效") from exc
    return _IMAGE_FORMATS[image_format]


def _read_image(path: Path) -> tuple[bytes, str, str]:
    with path.open("rb") as stream:
        data = stream.read(IMAGE_MAX_BYTES + 1)
    extension, media_type = validate_image(data)
    return data, extension, media_type


def _storage_directory() -> Path:
    root = config.DATA_DIR.resolve()
    directory = root / "library" / "images"
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("作品存储目录不能是符号链接")
    if not directory.resolve().is_relative_to(root):
        raise ValueError("作品存储目录超出受控范围")
    return directory


def _preserve(data: bytes, storage_key: str) -> bool:
    directory = _storage_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / storage_key
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise ValueError("作品原件冲突，请检查存储目录")
        return False
    descriptor, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def _validate_source(session_key, turn_id, metadata, event_key) -> str:
    if not isinstance(session_key, str):
        raise ValueError("session_key 必须是字符串")
    if turn_id is not None and (type(turn_id) is not int or turn_id < 1):
        raise ValueError("turn_id 必须是正整数或空值")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata 必须是对象")
    if event_key is not None and (not isinstance(event_key, str) or not event_key.strip()):
        raise ValueError("event_key 必须是非空字符串")
    return json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False)


def _source(connection, item_id, action, session_key, turn_id, metadata, event_key):
    connection.execute(
        "INSERT INTO library_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, item_id, action, session_key, turn_id, metadata, event_key, time.time()),
    )


def _upsert(connection, kind, identity, title, agent_id, url, storage_key) -> str:
    existing = connection.execute(
        "SELECT id FROM library_items WHERE identity=?", (identity,)
    ).fetchone()
    now = time.time()
    if existing:
        connection.execute(
            "UPDATE library_items SET url=?, updated_at=?, "
            "storage_key=CASE WHEN ?='' THEN storage_key ELSE ? END WHERE id=?",
            (url, now, storage_key, storage_key, existing["id"]),
        )
        return existing["id"]
    item_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO library_items (id,kind,identity,title,agent_id,url,storage_key,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (item_id, kind, identity, title, agent_id, url, storage_key, now, now),
    )
    return item_id


def _save_registration(connection, kind, identity, title, agent_id, url, storage_key,
                       session_key, turn_id, action, encoded, event_key, data, cover_path=None) -> dict:
    if cover_path is not None:
        data, storage_key = _read_cover(cover_path)
    created_file = False
    try:
        item_id = _upsert(connection, kind, identity, title, agent_id, url, storage_key)
        _source(connection, item_id, action, session_key, turn_id, encoded, event_key)
        if data is not None:
            created_file = _preserve(data, storage_key)
        item = _item(connection, item_id)
        connection.commit()
        return item
    except BaseException:
        # 仍持有写锁时回收本次新文件，不能删掉其他作品已保留的原件。
        if created_file:
            (config.DATA_DIR / "library" / "images" / storage_key).unlink(missing_ok=True)
        raise


def _register(kind, identity, title, url, storage_key, session_key, turn_id,
              action, metadata, event_key, data=None, cover_path=None) -> dict:
    if not isinstance(title, str) or len(title) > 1000:
        raise ValueError("标题必须是字符串，且不超过 1000 字")
    encoded = _validate_source(session_key, turn_id, metadata, event_key)
    owner = agents.agent_for_session(session_key) if session_key else None
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if event_key is not None:
            prior = connection.execute(
                "SELECT s.item_id,i.identity,s.action FROM library_sources s "
                "JOIN library_items i ON i.id=s.item_id WHERE event_key=?", (event_key,)
            ).fetchone()
            if prior:
                if prior["identity"] != identity or prior["action"] != action:
                    raise ValueError("event_key 已用于其他作品或动作")
                return _item(connection, prior["item_id"])
        return _save_registration(connection, kind, identity, title, owner["id"] if owner else "",
                                  url, storage_key, session_key, turn_id, action, encoded, event_key,
                                  data, cover_path)


def register_image(path, title='', session_key='', turn_id=None, action='sent',
                   metadata=None, event_key=None) -> dict:
    if not isinstance(action, str) or not action.strip():
        raise ValueError("来源动作不能为空")
    _validate_source(session_key, turn_id, metadata, event_key)
    data, extension, _ = _read_image(Path(path))
    digest = hashlib.sha256(data).hexdigest()
    storage_key = f"{digest}.{extension}"
    return _register("image", f"image:{digest}", title, "", storage_key,
                     session_key, turn_id, action, metadata, event_key, data)


def validate_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        raise ValueError("网站 URL 无效")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网站 URL 必须是无账号密码的 HTTP/HTTPS 地址")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("网站 URL 端口无效") from exc
    return url


def register_website(url, title, source_key, session_key='', turn_id=None,
                     metadata=None, event_key=None, cover_path=None) -> dict:
    validate_url(url)
    if not isinstance(source_key, str) or not source_key.strip():
        raise ValueError("网站 source_key 不能为空")
    return _register("website", f"website:{source_key}", title, url, "",
                     session_key, turn_id, "published", metadata, event_key, cover_path=cover_path)


def _read_cover(cover_path) -> tuple[bytes, str]:
    if not isinstance(cover_path, (str, Path)) or not str(cover_path).strip():
        raise ValueError("网站封面路径必须是非空字符串或 Path")
    path = Path(cover_path)
    if not path.is_absolute():
        raise ValueError("网站封面路径必须是绝对路径")
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError("网站封面路径必须是已有的普通图片文件，不能包含符号链接")
    data, extension, _ = _read_image(path)
    return data, f"cover-{hashlib.sha256(data).hexdigest()}.{extension}"


def _item(connection, item_id) -> dict | None:
    row = connection.execute("SELECT * FROM library_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return None
    item = {key: row[key] for key in ("id", "kind", "title", "agent_id", "url",
                                    "created_at", "updated_at")}
    item.update(pinned=bool(row["pinned"]), hidden=bool(row["hidden"]),
                file_url=f"/library/file?id={item_id}" if row["kind"] == "image" else "",
                cover_url=f"/library/cover?id={item_id}" if row["kind"] == "website"
                and row["storage_key"] else "",
                thumbnail_url=(f"/library/thumbnail?id={item_id}&v={_thumbnail_version(row['storage_key'])}"
                               if row["storage_key"] else ""))
    sources = connection.execute(
        "SELECT * FROM library_sources WHERE item_id=? ORDER BY created_at,id", (item_id,)
    ).fetchall()
    item["sources"] = [dict(source) for source in sources]
    for source in item["sources"]:
        source["metadata"] = json.loads(source["metadata"])
    return item


def get_item(id) -> dict | None:
    with _connect() as connection:
        return _item(connection, id)


def list_items(kind='', agent_id='', state='visible', search='', limit=60, offset=0) -> list[dict]:
    if kind not in {"", "image", "website"} or state not in {"visible", "pinned", "hidden", "all"}:
        raise ValueError("作品类型或状态无效")
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError("分页参数无效")
    clauses, values = [], []
    for column, value in (("kind", kind), ("agent_id", agent_id)):
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    if state != "all":
        clauses.append("hidden=?")
        values.append(int(state == "hidden"))
    if state == "pinned":
        clauses.append("pinned=1")
    if search:
        clauses.append("(instr(lower(title),lower(?))>0 OR instr(lower(url),lower(?))>0)")
        values.extend([search, search])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as connection:
        rows = connection.execute("SELECT id FROM library_items" + where +
                                  " ORDER BY pinned DESC,updated_at DESC,id LIMIT ? OFFSET ?",
                                  [*values, limit, offset]).fetchall()
        return [_item(connection, row["id"]) for row in rows]


def update_item(id, **fields) -> dict:
    if not fields or fields.keys() - {"title", "pinned", "hidden", "agent_id"}:
        raise ValueError("仅可修改 title、pinned、hidden、agent_id")
    for key, value in fields.items():
        if key in {"pinned", "hidden"} and type(value) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
        if key in {"title", "agent_id"} and (not isinstance(value, str) or len(value) > 1000):
            raise ValueError(f"{key} 必须是字符串，且不超过 1000 字")
    if fields.get("agent_id") and agents.get(fields["agent_id"]) is None:
        raise ValueError("Agent 不存在")
    with _connect() as connection:
        assignments = ",".join(f"{key}=?" for key in fields)
        cursor = connection.execute(f"UPDATE library_items SET {assignments},updated_at=? WHERE id=?",
                                    [*fields.values(), time.time(), id])
        if not cursor.rowcount:
            raise KeyError("作品不存在")
        return _item(connection, id)


def _thumbnail_version(storage_key: str) -> str:
    # 列表只读文件属性，不读取或解码原件；异常原件仍由图片接口明确报错。
    fingerprint = storage_key
    try:
        directory = _storage_directory()
        path = directory / storage_key
        if Path(storage_key).name == storage_key and not path.is_symlink():
            stat = path.stat()
            fingerprint += f":{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}:{stat.st_ino}"
    except (OSError, ValueError):
        pass
    return hashlib.sha256(f"{fingerprint}:{_THUMBNAIL_PARAMETERS}".encode()).hexdigest()


def _thumbnail_directory() -> Path:
    directory = _storage_directory().parent / "thumbnails"
    if directory.is_symlink():
        raise ValueError("缩略图缓存目录不能是符号链接")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _cached_thumbnail(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError("缩略图缓存路径不能是符号链接")
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return None
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "WEBP" or max(image.size) > THUMBNAIL_SIZE:
                return None
            image.load()
        return data
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _encode_thumbnail(path: Path) -> bytes:
    data, _, _ = _read_image(path)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            image.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
            image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=80)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("图片内容无效") from exc


def _write_thumbnail(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _thumbnail_source(item_id: str) -> tuple[Path, str]:
    with _connect() as connection:
        row = connection.execute("SELECT kind FROM library_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise KeyError("作品不存在")
    path = _stored_file(item_id, row["kind"])
    return path, _thumbnail_version(path.name)


def thumbnail_data(item_id: str, version: str | None = None) -> tuple[bytes, str]:
    if version is not None and (len(version) != 64 or any(c not in "0123456789abcdef" for c in version)):
        raise ValueError("缩略图版本无效")
    source, current = _thumbnail_source(item_id)
    if version is not None and version != current:
        raise ThumbnailVersionError("缩略图版本已更新，请刷新作品列表")
    lock = _THUMBNAIL_LOCKS[int(current[:8], 16) % len(_THUMBNAIL_LOCKS)]
    with lock:
        path = _thumbnail_directory() / f"{current}.webp"
        data = _cached_thumbnail(path)
        if data is None:
            data = _encode_thumbnail(source)
            if _thumbnail_source(item_id) != (source, current):
                raise ThumbnailVersionError("缩略图版本已更新，请刷新作品列表")
            _write_thumbnail(path, data)
        if _thumbnail_source(item_id) != (source, current):
            raise ThumbnailVersionError("缩略图版本已更新，请刷新作品列表")
        return data, current


def image_file(id) -> Path:
    return _stored_file(id, "image")


def cover_file(id) -> Path:
    return _stored_file(id, "website")


def _stored_file(id, kind) -> Path:
    with _connect() as connection:
        row = connection.execute("SELECT kind,storage_key FROM library_items WHERE id=?", (id,)).fetchone()
    if row is None:
        raise KeyError("作品不存在")
    if row["kind"] != kind:
        raise ValueError("该作品不是图片" if kind == "image" else "该作品不是网站")
    if kind == "website" and not row["storage_key"]:
        raise FileNotFoundError("网站没有封面")
    directory = _storage_directory()
    key = row["storage_key"]
    path = directory / key
    if not key or Path(key).name != key or path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise ValueError("作品原件路径无效")
    if not path.is_file():
        raise FileNotFoundError("作品原件已丢失")
    return path
