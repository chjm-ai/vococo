"""独立、可测试的作品库 HTTP 接口。"""
from __future__ import annotations

import asyncio
import base64
from functools import wraps
import hashlib
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from aiohttp import web

from .. import config
from ..memory import agents, library, session_store
from .adapters.base import ImageAttachment
from .web_auth import check_web_auth


def _endpoint(handler):
    @wraps(handler)
    async def wrapped(request):
        denied = check_web_auth(request)
        if denied is not None:
            return denied
        try:
            return await handler(request)
        except (KeyError, FileNotFoundError) as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            request.app.logger.exception("作品库请求失败")
            return web.json_response({"error": "作品库读取或保存失败，请重试"}, status=500)
    return wrapped


def _id(request) -> str:
    item_id = request.query.get("id", "")
    if not item_id:
        raise ValueError("缺少作品 id")
    return item_id


def _source_conv(session_key: str) -> str:
    if session_key == config.resolve_session_key("web", "main"):
        return "main"
    if session_key.startswith("web:"):
        candidate = session_key[4:]
    elif session_key.startswith(("task:", "voice-chat:")):
        candidate = session_key
    else:
        return ""
    return candidate if candidate and config.resolve_session_key("web", candidate) == session_key else ""


def _local_open_url(row: dict) -> str:
    if row["kind"] != "website" or not any(
        source.get("metadata", {}).get("target") == "local" for source in row["sources"]
    ):
        return ""
    parsed = urlsplit(library.validate_url(row["url"]))
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.path.startswith("/pub/"):
        return ""
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
        return ""
    # 返回同源路径，不信任请求 Host，也不改存储中的发布凭据/正式地址。
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


def _public_item(row: dict) -> dict:
    for source in row["sources"]:
        source["conv"] = _source_conv(source["session_key"])
        deleted = bool(source.get("deleted") or source.get("metadata", {}).get("deleted"))
        if source["conv"] and session_store.find_session(source["session_key"]) is None:
            deleted = True
        source["can_open"] = bool(source["conv"]) and not deleted
        if deleted:
            source["deleted"] = True
    open_url = _local_open_url(row)
    if open_url:
        row["open_url"] = open_url
    return row


@_endpoint
async def items(request):
    query = request.query
    rows = await asyncio.to_thread(
        library.list_items, kind=query.get("kind", ""), agent_id=query.get("agent_id", ""),
        state=query.get("state", "visible"), search=query.get("search", ""),
        limit=int(query.get("limit", "60")), offset=int(query.get("offset", "0")),
    )
    owners = await asyncio.to_thread(agents.list_agents)
    return web.json_response({"items": [_public_item(row) for row in rows], "agents": [
        {"id": owner["id"], "name": owner["name"]} for owner in owners
    ]})


@_endpoint
async def item(request):
    row = await asyncio.to_thread(library.get_item, _id(request))
    if row is None:
        raise KeyError("作品不存在")
    return web.json_response(_public_item(row))


@_endpoint
async def patch_item(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    row = await asyncio.to_thread(library.update_item, _id(request), **body)
    return web.json_response(_public_item(row))


@_endpoint
async def file(request):
    path = await asyncio.to_thread(library.image_file, _id(request))
    _, _, media_type = await asyncio.to_thread(library._read_image, path)
    return web.FileResponse(path, headers={"Content-Type": media_type,
                                         "Cache-Control": "private, no-store",
                                         "X-Content-Type-Options": "nosniff"})


def resolve_refs(refs) -> tuple[list[ImageAttachment], list[str]]:
    if not isinstance(refs, list) or len(refs) > 20:
        raise ValueError("library_refs 必须是作品 id 数组，最多 20 个")
    images, links, seen = [], [], set()
    total = 0
    for item_id in refs:
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            if isinstance(item_id, str) and item_id in seen:
                continue
            raise ValueError("library_refs 只接受作品 id，不接受路径或对象")
        seen.add(item_id)
        row = library.get_item(item_id)
        if row is None:
            raise KeyError(f"作品不存在：{item_id}")
        if row["kind"] == "website":
            links.append(library.validate_url(row["url"]))
            continue
        data, _, media_type = library._read_image(library.image_file(item_id))
        total += len(data)
        if total > library.IMAGE_MAX_BYTES:
            raise ValueError("引用图片总大小超过 20MB 上限")
        images.append(ImageAttachment(data=base64.b64encode(data).decode("ascii"), media_type=media_type))
    return images, links


def _controlled(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        return False
    return path.is_file()


def _history_images(root: Path) -> list[dict]:
    if not root.is_dir() or root.is_symlink():
        return []
    grouped = {}
    for path in sorted(root.iterdir()):
        if not path.name.startswith(("gen_", "ai_")) or not _controlled(path, root):
            continue
        try:
            data, _, _ = library._read_image(path)
        except (OSError, ValueError):
            continue
        digest = hashlib.sha256(data).hexdigest()
        candidate = grouped.setdefault(digest, {"kind": "image", "title": "", "agent_id": "",
                                               "hash": digest, "files": []})
        candidate["files"].append(path.name)
    return list(grouped.values())


def _history_websites(root: Path) -> list[dict]:
    if not root.is_dir() or root.is_symlink():
        return []
    candidates = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        path = entry / "index.html" if entry.is_dir() else entry
        if path.suffix.lower() not in {".html", ".htm"} or not _controlled(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        candidates.append({"kind": "website", "title": "", "agent_id": "",
                           "source_key": f"local-pub:{relative}",
                           "url": f"/pub/{quote(relative, safe='/')}", "file": relative})
    return candidates


def history_candidates() -> list[dict]:
    # 预览只读受控目录；mtime 不是发布时间，未知归属不从文件名推断。
    return _history_images(config.IMAGES_DIR) + _history_websites(config.PUBLISHED_DIR)


@_endpoint
async def history_preview(request):
    return web.json_response({"candidates": await asyncio.to_thread(history_candidates)})


def register_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/library/items", items), web.get("/library/item", item),
        web.patch("/library/item", patch_item), web.get("/library/file", file),
        web.post("/library/history/preview", history_preview),
    ])
