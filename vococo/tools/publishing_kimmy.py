"""Kimmy 静态适配器：固定 SSH 通道、备份和变更检查，复用原 deploy.sh。"""
from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path

from .. import config

# 2026-10-13 只读核对过的脚本；远端更新脚本后必须重新审计，不能静默执行新契约。
_DEPLOY_SHA256 = "da143a46b9b576b501c895f837e01dda5e34d00bdfc36b5c741bf681510d8e7f"
_REMOTE = r'''
import fcntl, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
phase, slug, version = sys.argv[1:]
if phase not in {"inspect", "prepare", "activate"} or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not re.fullmatch(r"[a-f0-9]{32}", version):
    raise ValueError("非法发布参数")
root = Path("/opt/deploy")
incoming, live = root / "incoming" / slug, Path("/var/www") / slug
stage = root / "vococo-staging" / (slug + "-" + version)
record = root / "vococo-state" / (slug + ".json")
backup = root / "vococo-backups" / slug / version
snippet = Path("/etc/nginx/snippets/pub.chjm.cc") / (slug + ".conf")
script = root / "deploy.sh"
expected_hash = "DEPLOY_HASH"
upload_expected = UPLOAD_MANIFEST

def contract():
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("部署脚本已变更，须重新审计契约")
    lines = (root / "config").read_text().splitlines()
    expected = {"DOMAIN=pub.chjm.cc", "URL_MODE=path",
                "SSL_CERT=/etc/letsencrypt/live/site.speeedai.com/fullchain.pem",
                "SSL_KEY=/etc/letsencrypt/live/site.speeedai.com/privkey.pem"}
    if set(lines) != expected or len(lines) != len(expected):
        raise ValueError("发布配置已变更或含未审计指令")
    if not Path("/usr/sbin/nginx").is_file() and not Path("/usr/bin/nginx").is_file():
        raise ValueError("nginx 通道不可用，禁止切换部署系统")

def manifest(path):
    if path.is_symlink():
        raise ValueError("站点根目录不能是软链接")
    if not path.exists():
        return None
    if not path.is_dir():
        raise ValueError("站点路径不是目录")
    result, total = {}, 0
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError("远端站点包含软链接，拒绝覆盖")
        relative = child.relative_to(path)
        if ".git" in relative.parts:
            continue
        if child.is_dir():
            continue
        if not child.is_file():
            raise ValueError("站点包含非普通文件")
        total += child.stat().st_size
        if total > 104857600 or len(result) >= 2000:
            raise ValueError("远端站点超出安全备份范围")
        result[relative.as_posix()] = hashlib.sha256(child.read_bytes()).hexdigest()
    return result

def git_clean(path):
    if not path.exists():
        return
    roots = [path] if (path / ".git").exists() else []
    roots += [entry.parent for entry in path.rglob(".git") if entry.parent != path]
    for repository in roots:
        status = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "safe.directory=" + str(repository), "-C", str(repository),
                                 "status", "--porcelain", "--untracked-files=all", "--ignored"], capture_output=True, text=True)
        if status.returncode or status.stdout.strip():
            raise ValueError("远端有未提交或未跟踪的 git 改动，拒绝覆盖")

def inspect():
    contract()
    git_clean(incoming)
    git_clean(live)
    state = {"incoming": manifest(incoming), "live": manifest(live),
             "snippet": hashlib.sha256(snippet.read_bytes()).hexdigest() if snippet.is_file() else None}
    if record.is_symlink():
        raise ValueError("发布记账文件不能是软链接")
    if record.exists():
        baseline = json.loads(record.read_text())
        if state != baseline:
            raise ValueError("远端有未登记改动，拒绝覆盖；先保留改动或使用新 slug")
    elif state["incoming"] != state["live"] or (state["live"] is None and state["snippet"] is not None):
        raise ValueError("首次接管站点的 incoming 与在线文件不一致，不能确认服务器更新，拒绝覆盖")
    return state

try:
    lock_dir = root / "vococo-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = (lock_dir / (slug + ".lock")).open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    if phase == "inspect":
        print(json.dumps({"state": inspect()}))
    elif phase == "prepare":
        state = inspect()
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.mkdir(mode=0o700)
        (stage / ".expected-state.json").write_text(json.dumps(state))
        (stage / ".expected-upload.json").write_text(json.dumps(upload_expected))
        print(json.dumps({"stage": str(stage)}))
    else:
        expected = json.loads((stage / ".expected-state.json").read_text())
        upload = stage / "site"
        if not (upload / "index.html").is_file():
            raise ValueError("上传目录缺 index.html")
        uploaded_manifest = manifest(upload)
        if uploaded_manifest != json.loads((stage / ".expected-upload.json").read_text()):
            raise ValueError("远端上传内容与已审批快照不一致，拒绝激活")
        if inspect() != expected:
            raise ValueError("远端在上传期间发生变更，拒绝覆盖")
        backup.mkdir(parents=True, mode=0o700)
        for name, path in (("incoming", incoming), ("live", live)):
            if path.exists():
                shutil.copytree(path, backup / name, symlinks=True)
        if snippet.exists():
            shutil.copy2(snippet, backup / "nginx.conf")
        (backup / "before.json").write_text(json.dumps(expected))
        if inspect() != expected:
            raise ValueError("远端在备份期间发生变更，拒绝覆盖")
        incoming.parent.mkdir(parents=True, exist_ok=True)
        if incoming.exists():
            incoming.rename(backup / "incoming-original")
        upload.rename(incoming)
        deployed = subprocess.run(["bash", str(script), slug, "static"], capture_output=True, text=True)
        if deployed.returncode:
            print(json.dumps({"activation": "unknown", "error": deployed.stderr[-2000:] or deployed.stdout[-2000:],
                              "backup_path": str(backup), "message": "脚本失败但可能已改动在线文件；不得盲目重试发布，请核对备份"}))
        else:
            after = {"incoming": manifest(incoming), "live": manifest(live),
                     "snippet": hashlib.sha256(snippet.read_bytes()).hexdigest() if snippet.is_file() else None}
            if after["live"] != uploaded_manifest:
                raise ValueError("部署后在线内容与上传不一致；已保留备份，激活状态待核对")
            warning = ""
            try:
                record.parent.mkdir(parents=True, exist_ok=True)
                temporary = record.with_suffix(".json.next")
                temporary.write_text(json.dumps(after))
                temporary.replace(record)
            except OSError as exc:
                warning = str(exc)
            print(json.dumps({"activation": "succeeded", "backup_path": str(backup), "manifest_warning": warning}))
except Exception as exc:
    print(json.dumps({"activation": "unknown" if phase == "activate" else "failed", "error": str(exc),
                      "backup_path": str(backup) if backup.exists() else ""}))
'''.replace("DEPLOY_HASH", _DEPLOY_SHA256)


async def _command(argv: list[str], stdin: bytes | None = None) -> str:
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=180)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("SSH/上传超时；若处于激活阶段，结果未知，不要自动重新发布") from None
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-2000:] or "远端命令失败")
    return stdout.decode()


async def _remote(phase: str, slug: str, version: str, upload_manifest: dict | None = None) -> dict:
    command = shlex.join(["sudo", "-n", "python3", "-", phase, slug, version])
    output = await _command(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "ConnectTimeout=10", "Kimmy", command],
                             _REMOTE.replace("UPLOAD_MANIFEST", repr(upload_manifest)).encode())
    result = json.loads(output)
    if result.get("error") and phase != "activate":
        raise ValueError(result["error"])
    return result


async def _verify(url: str) -> dict:
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as response:
                return {"http_verification": "verified" if response.status == 200 else "pending",
                        "http_status": response.status}
    except Exception as exc:
        return {"http_verification": "pending", "http_error": str(exc)}


async def publish(source: Path, slug: str, approved_manifest: dict) -> dict:
    from .publishing import _manifest

    version = uuid.uuid4().hex
    staging = config.DATA_DIR / "publish_staging"
    staging.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"kimmy-{slug}-", dir=staging))
    result = None
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)
        if _manifest(stage) != approved_manifest:
            raise ValueError("来源目录在审批期间发生改动，请核对后重新发布")
        # openrsync 的 --chmod 兼容性不可靠；只规范临时副本，再由 -a 保留权限。
        stage.chmod(0o755)
        for path in stage.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        await _remote("inspect", slug, version)
        prepared = await _remote("prepare", slug, version, approved_manifest)
        expected_stage = f"/opt/deploy/vococo-staging/{slug}-{version}"
        if prepared.get("stage") != expected_stage:
            raise ValueError("远端临时目录回执不符合白名单")
        # 上传到全新私有临时目录，不对正在服务的站点做盲目 --delete。
        # sudo 创建的临时目录由固定 sudo rsync 接收，不扩大来源或目标路径。
        await _command(["rsync", "-az", "--rsync-path=sudo -n rsync",
                        "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10",
                        str(stage) + "/", f"Kimmy:{expected_stage}/site/"])
        try:
            activated = await _remote("activate", slug, version)
        except Exception as exc:
            activated = {"activation": "unknown", "error": str(exc),
                         "backup_path": f"/opt/deploy/vococo-backups/{slug}/{version}",
                         "message": "激活回执中断，可能已部署；先核对状态和备份，不要自动重新发布"}
        if activated.get("activation") != "succeeded":
            result = {**activated, "url": f"https://pub.chjm.cc/{slug}/", "event_key": f"publish:{version}"}
            return result
        metadata = {"target": "kimmy", "slug": slug, "version": version,
                    "activation": "succeeded", "http_verification": "not_checked",
                    "backup_path": activated["backup_path"], "script_sha256": _DEPLOY_SHA256}
        if activated.get("manifest_warning"):
            metadata["manifest_warning"] = activated["manifest_warning"]
        metadata.update(await _verify(f"https://pub.chjm.cc/{slug}/"))
        result = {"url": f"https://pub.chjm.cc/{slug}/", "source_key": f"publish:kimmy:{slug}",
                  "activation": "succeeded", "metadata": metadata, "event_key": f"publish:{version}"}
        return result
    finally:
        try:
            shutil.rmtree(stage)
        except OSError as exc:
            # 清理不影响已成功的发布和登记；保留路径供后续清理，绝不重新部署。
            warning = {"path": str(stage), "error": str(exc)}
            if result is not None:
                result.setdefault("metadata", {})["local_cleanup_warning"] = warning
