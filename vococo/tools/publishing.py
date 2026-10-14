"""受控网站发布：激活和 HTTP 验活分开，不接受 shell 或远端地址。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import os
import hashlib
import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .. import config
from . import danger

_LOCKS: dict[str, asyncio.Lock] = {}


class ActivationNotStartedError(ValueError):
    """已确认失败发生在触碰在线站点之前，可以恢复原可信状态。"""


class ActivationNotStartedCancelledError(asyncio.CancelledError):
    """取消仍需向上传播，只额外标记尚未触碰在线站点。"""


def _private_path(name: str) -> Path:
    path = config.DATA_DIR / name
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("发布状态或备份路径不能包含软链接")
    if path.resolve().is_relative_to(config.PUBLISHED_DIR.resolve()):
        raise ValueError("发布状态及备份必须位于公开目录之外")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def publication_guard(source_key: str):
    match = re.fullmatch(r'publish:(local|kimmy):([a-z0-9]+(?:-[a-z0-9]+)*)', source_key)
    if not match:
        raise ValueError('仅支持受控站点的封面写入锁')
    _state_path(match[1], match[2])
    path = _private_path('publish_locks') / f'{match[1]}-{match[2]}.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@asynccontextmanager
async def _site_lock(target: str, slug: str):
    async with _LOCKS.setdefault(f"{target}:{slug}", asyncio.Lock()):
        path = _private_path('publish_locks') / f'{target}-{slug}.lock'
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(descriptor)


def _state_path(target: str, slug: str) -> Path:
    if target not in {"local", "kimmy"} or not isinstance(slug, str) or len(slug) > 64 or not _SLUG.fullmatch(slug):
        raise ValueError("下线目标或 slug 无效，仅支持 local/kimmy 和安全站点名")
    path = _private_path("publish_states") / f"{target}-{slug}.json"
    if path.is_symlink():
        raise ValueError("发布状态不能是软链接")
    return path


def publication_state(source_key: str) -> dict | None:
    match = re.fullmatch(r"publish:(local|kimmy):([a-z0-9]+(?:-[a-z0-9]+)*)", source_key)
    if not match:
        return None
    path = _state_path(match[1], match[2])
    state = json.loads(path.read_text()) if path.exists() else None
    if state and state.get('status') == 'prepared' and match[1] == 'local':
        destination = config.PUBLISHED_DIR / match[2]
        backup = Path(state['backup_path'])
        record = config.DATA_DIR / 'publish_manifests' / f'{match[2]}.json'
        if (not destination.exists() and not destination.is_symlink() and backup.is_dir()
                and not backup.is_symlink() and backup.parent == _private_path('publish_backups')
                and not record.is_symlink() and record.is_file()
                and _manifest(backup) == json.loads(record.read_text())):
            return {**state, 'status': 'inactive'}
        return state.get('previous')
    return state


def _write_state(target: str, slug: str, state: dict) -> None:
    path = _state_path(target, slug)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _offline_local(slug: str, previous: dict | None) -> dict:
    root = config.PUBLISHED_DIR
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("公开目录不能包含软链接")
    destination = root / slug
    record = config.DATA_DIR / "publish_manifests" / f"{slug}.json"
    if record.is_symlink():
        raise ValueError("发布记账不能是软链接")
    if not destination.exists() and not destination.is_symlink():
        raise ValueError("站点不存在，且没有可信下线回执；未移除作品库记录")
    _check_existing(destination, record)
    token = uuid.uuid4().hex
    backup = _private_path("publish_backups") / f"{slug}-offline-{token}"
    state = {"status": "prepared", "token": token, "backup_path": str(backup), "previous": previous}
    _write_state("local", slug, state)
    try:
        _check_existing(destination, record)
        destination.rename(backup)
    except BaseException:
        _restore_state('local', slug, previous)
        raise
    return {**state, "status": "inactive"}


def _sync_offline(target: str, slug: str, state: dict) -> dict:
    from ..memory import library

    result = {"deactivation": "succeeded", "backup_path": state["backup_path"]}
    try:
        library.deactivate_website(f"publish:{target}:{slug}", state["token"])
        result["library_sync"] = "succeeded"
    except Exception as exc:
        result.update(library_sync="failed", error=f"站点已下线，但作品库同步失败：{exc}",
                      retry={"target": target, "slug": slug, "sync_token": state["token"]})
    return result


async def unpublish(args: dict) -> dict:
    target, slug = args.get("target", ""), args.get("slug", "")
    _state_path(target, slug)
    async with _site_lock(target, slug):
        state = publication_state(f"publish:{target}:{slug}")
        token = args.get("sync_token")
        if token is not None:
            if not state or state.get("status") != "inactive" or token != state.get("token"):
                raise ValueError("同步凭据已失效；不会再次下线或修改作品库")
            return _sync_offline(target, slug, state)
        if state and state.get("status") == "inactive":
            return _sync_offline(target, slug, state)
        if not await danger.require_approval("下线网站(SSH/移出公开目录/停用作品)",
                                             f"目标 {target}；站点 {slug}；核对受控内容后移入非公开备份，可恢复"):
            return {"deactivation": "denied", "error": "下线未获批准，未改动站点或作品库"}
        if target == "local":
            state = _offline_local(slug, state)
        else:
            from . import publishing_kimmy
            result = await publishing_kimmy.unpublish(slug)
            if result.get("deactivation") != "succeeded":
                return result
            state = {"status": "inactive", "token": result["token"], "backup_path": result["backup_path"]}
        return _finish_offline(target, slug, state)


def _finish_offline(target: str, slug: str, state: dict) -> dict:
    try:
        _write_state(target, slug, state)
    except OSError as exc:
        recovered = publication_state(f'publish:{target}:{slug}')
        if not recovered or recovered.get('status') != 'inactive' or recovered.get('token') != state['token']:
            return {'deactivation': 'unknown', 'error': f'站点可能已下线，但可信回执保存失败：{exc}；请核对备份',
                    'backup_path': state['backup_path']}
        result = _sync_offline(target, slug, recovered)
        result['state_warning'] = str(exc)
        return result
    return _sync_offline(target, slug, state)


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_BYTES = 100 * 1024 * 1024
_MAX_FILES = 2000
_DENIED_NAMES = {".git", ".env", ".ssh", ".aws", "node_modules", ".venv", "__pycache__",
                 "credentials.json", "credentials.txt", "service-account.json", "secrets.json"}
_SECRET_CONTENT = re.compile(rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|"
                             rb'[\"\'](?:private_key|client_secret|aws_secret_access_key|api_key)[\"\']\s*[:=]', re.I)
_PUBLIC_EXTENSIONS = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg",
                      ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".txt", ".xml",
                      ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".pdf"}


def _manifest(source: Path) -> dict[str, str]:
    entries, size = {}, 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"发布目录不能包含软链接：{path.name}")
        if any(part in _DENIED_NAMES or part.startswith(".env.") for part in path.relative_to(source).parts):
            raise ValueError(f"发布目录包含私密文件或依赖目录：{path.name}；请准备干净的静态产物目录")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix.lower() not in _PUBLIC_EXTENSIONS:
            raise ValueError("发布目录只能包含白名单静态文件；不允许密钥、数据库或可执行脚本")
        size += path.stat().st_size
        if size > _MAX_BYTES or len(entries) >= _MAX_FILES:
            raise ValueError("发布目录超过 100MB 或 2000 个文件")
        content = path.read_bytes()
        if _SECRET_CONTENT.search(content):
            raise ValueError(f"发布目录包含疑似凭据：{path.name}；请移除后重试")
        entries[path.relative_to(source).as_posix()] = hashlib.sha256(content).hexdigest()
    return entries


def _source_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute() or path.is_symlink():
        raise ValueError("source_dir 必须是已有目录的绝对路径，不能是软链接")
    path = path.resolve()
    if path in {Path("/"), Path.home(), Path(danger.current_cwd() or Path.cwd()).resolve()}:
        raise ValueError("不能发布根目录、家目录或整个当前仓库；请准备静态产物子目录")
    if not path.is_dir() or not (path / "index.html").is_file():
        raise ValueError("静态发布目录必须包含 index.html")
    return path


def _check_existing(destination: Path, manifest_file: Path) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    if destination.is_symlink() or not manifest_file.is_file():
        raise ValueError("已有站点不受此发布器管理，拒绝覆盖；请使用新 slug")
    recorded = json.loads(manifest_file.read_text(encoding="utf-8"))
    if _manifest(destination) != recorded:
        raise ValueError("已发布目录有未登记的改动，拒绝覆盖；请先保留这些改动或使用新 slug")


def _activate(source: Path, slug: str, version: str, approved_manifest: dict) -> dict:
    root = config.PUBLISHED_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifests = config.DATA_DIR / "publish_manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    destination, record = root / slug, manifests / f"{slug}.json"
    _check_existing(destination, record)
    staging_root = config.DATA_DIR / "publish_staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{slug}-", dir=staging_root))
    try:
        # 保留软链接再校验，绝不把审批等待期间新加入的链接解引用进公开目录。
        shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)
        snapshot = _manifest(stage)
        if snapshot != approved_manifest:
            raise ValueError("来源目录在审批期间发生改动，请核对后重新发布")
        _check_existing(destination, record)
        backup = _private_path("publish_backups") / f"{slug}-{version}"
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except Exception:
            if backup.exists():
                backup.rename(destination)
            raise
        # 记账文件在公开目录外；失败时保留已激活的站点，不重做发布。
        metadata = {"target": "local", "slug": slug, "version": version,
                    "activation": "succeeded", "http_verification": "not_checked",
                    "local_path": f"/pub/{slug}/"}
        try:
            record.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            metadata["manifest_warning"] = str(exc)
        return metadata
    finally:
        if stage.exists():
            shutil.rmtree(stage)


async def _activate_local(source: Path, slug: str, version: str, approved_manifest: dict) -> dict:
    operation = asyncio.create_task(asyncio.to_thread(_activate, source, slug, version, approved_manifest))
    cancellation_requested = False
    while True:
        try:
            metadata = await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            if operation.cancelled():
                raise
            # 线程不能取消；写入和记账结束前不得释放锁。成功后继续返回回执，
            # 让调用方完成同步作品库登记，避免已激活却被当成未发布重试。
            cancellation_requested = True
    if cancellation_requested:
        metadata["cancellation_requested"] = True
    return metadata


def _restore_state(target: str, slug: str, previous: dict | None) -> None:
    if previous is None:
        _state_path(target, slug).unlink(missing_ok=True)
    else:
        _write_state(target, slug, previous)


async def _publish_receipt(source: Path, target: str, slug: str, approved_manifest: dict) -> dict:
    if target == 'kimmy':
        from . import publishing_kimmy
        return await publishing_kimmy.publish(source, slug, approved_manifest)
    version = uuid.uuid4().hex
    try:
        metadata = await _activate_local(source, slug, version, approved_manifest)
    except ValueError as exc:
        raise ActivationNotStartedError(str(exc)) from exc
    return {'url': f'http://127.0.0.1:{config.WEB_PORT}/pub/{slug}/',
            'source_key': f'publish:local:{slug}', 'metadata': metadata,
            'activation': 'succeeded', 'event_key': f'publish:{version}'}


async def publish(args: dict) -> dict:
    target, slug = args.get("target", ""), args.get("slug", "")
    if target not in {"local", "kimmy", "site"}:
        raise ValueError("target 仅支持 local / kimmy / site")
    if not isinstance(slug, str) or len(slug) > 64 or not _SLUG.fullmatch(slug):
        raise ValueError("slug 必须是 1–64 位小写英文、数字或短横线")
    if args.get("type", "static") != "static":
        raise ValueError("当前仅可靠支持 static；Node/Python 的端口和激活契约尚未接入")
    if target == "site":
        raise ValueError("未支持 site：现有脚本会清理旧 release，且缺少在线目录未登记改动保护；不会切换部署系统")
    source = _source_dir(args.get("source_dir", ""))
    approved_manifest = _manifest(source)
    if source == config.PUBLISHED_DIR.resolve() or source.is_relative_to(config.PUBLISHED_DIR.resolve()):
        raise ValueError("source_dir 不能位于发布目录内")
    channel = ("Kimmy SSH + rsync 上传 + /opt/deploy/deploy.sh；公开地址 https://pub.chjm.cc/"
               if target == "kimmy" else "本机 /pub/（不要求登录，绑定外网或隧道时可公开访问）")
    detail = f"目标 {target}；站点 {slug}；来源 {source}；{channel}；先备份，拒绝覆盖未登记/未提交改动"
    if not await danger.require_approval("发布网站(SSH/上传/公开文件/覆盖站点)", detail):
        return {"activation": "denied", "error": "发布未获批准，未改动站点"}
    async with _site_lock(target, slug):
        previous = publication_state(f'publish:{target}:{slug}')
        if previous and previous.get('status') == 'publishing':
            previous = previous.get('previous')
        _write_state(target, slug, {'status': 'publishing', 'previous': previous})
        try:
            receipt = await _publish_receipt(source, target, slug, approved_manifest)
        except (ActivationNotStartedError, ActivationNotStartedCancelledError):
            # 仅恢复已确认未进入激活阶段的失败或取消，随后原样向上传播。
            _restore_state(target, slug, previous)
            raise
        if receipt.get('activation') in {'denied', 'failed'}:
            _restore_state(target, slug, previous)
        if receipt.get("activation") == "succeeded":
            try:
                _write_state(target, slug, {"status": "active", "receipt": receipt})
            except OSError as exc:
                return {**receipt, 'activation': 'unknown',
                        'error': f'站点已激活，但可信状态写入失败：{exc}；请核对，不要盲目重新发布'}
        return receipt
