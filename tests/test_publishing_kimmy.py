"""Kimmy契约在临时文件树验证；SSH、rsync、HTTP均替换，不部署公网。"""
import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from deployment_fixture import deployment_config

from vococo.tools import builtin, publishing, publishing_kimmy as kimmy


def run_tool(args):
    text = asyncio.run(builtin.publish_website.handler(args))["content"][0]["text"]
    return text, json.loads(text.split("\n", 1)[1])


@pytest.fixture(autouse=True)
def isolate_cover_network(monkeypatch):
    from vococo.tools import website_preview
    async def capture(*args):
        raise RuntimeError("隔离测试不连接公网")
    monkeypatch.setattr(website_preview, '_capture', capture)


@pytest.fixture
def source(isolated):
    directory = isolated / "static"
    directory.mkdir()
    (directory / "index.html").write_text("new site")
    return directory


@pytest.fixture
def params(source):
    return {"target": "kimmy", "slug": "sample", "source_dir": str(source), "title": "示例", "type": "static"}


@pytest.mark.parametrize("http,cleanup_fails", [("verified", False), ("pending", False), ("verified", True)])
def test_kimmy_requires_approval_and_registers_even_when_http_pending(isolated, params, monkeypatch, http, cleanup_fails):
    from vococo.memory import library
    calls = []
    async def approve(reason, detail):
        calls.append("approval")
        assert "SSH" in reason and "fake-publisher" in detail
        return True
    async def remote(phase, slug, version, upload_manifest=None, **kwargs):
        assert calls[0] == "approval"
        calls.append(phase)
        if phase == "prepare":
            assert upload_manifest["index.html"]
            return {"stage": f"/srv/fake-deploy/vococo-staging/{slug}-{version}"}
        if phase == "activate":
            return {"activation": "succeeded", "backup_path": f"/srv/fake-deploy/vococo-backups/{slug}/{version}"}
        return {"state": {}}
    async def command(argv, stdin=None):
        calls.append("upload")
        assert argv[0] == "rsync" and "--delete" not in argv
        assert argv[-1].startswith("fake-publisher:/srv/fake-deploy/vococo-staging/sample-")
        return ""
    async def verify(url):
        assert url == "https://pub.example.com/sample/"
        return {"http_verification": http}
    monkeypatch.setattr(publishing.danger, "require_approval", approve)
    monkeypatch.setattr(kimmy, "_remote", remote)
    monkeypatch.setattr(kimmy, "_command", command)
    monkeypatch.setattr(kimmy, "_verify", verify)
    if cleanup_fails:
        original_cleanup = shutil.rmtree
        def cleanup(path, *args, **kwargs):
            if Path(path).name.startswith("kimmy-sample-"):
                raise PermissionError("read-only staging")
            return original_cleanup(path, *args, **kwargs)
        monkeypatch.setattr(shutil, "rmtree", cleanup)
    text, receipt = run_tool(params)
    assert calls == ["approval", "inspect", "prepare", "upload", "activate"]
    assert receipt["library_id"]
    assert receipt["metadata"]["http_verification"] == http
    assert receipt["source_key"] == "publish:kimmy:sample"
    assert library.get_item(receipt["library_id"])["url"] == "https://pub.example.com/sample/"
    if http == "pending":
        assert "访问待验证" in text
    if cleanup_fails:
        assert receipt["activation"] == "succeeded"
        assert receipt["metadata"]["local_cleanup_warning"]["error"] == "read-only staging"


def test_kimmy_upload_with_system_rsync_preserves_public_modes_and_source(source, monkeypatch, tmp_path):
    import stat
    import subprocess

    if shutil.which("rsync") is None:
        pytest.skip("系统未安装 rsync")
    source.chmod(0o700)
    source.joinpath("index.html").chmod(0o600)
    nested = source / "assets"
    nested.mkdir(mode=0o700)
    asset = nested / "app.js"
    asset.write_text("test")
    asset.chmod(0o700)
    (source / "empty").mkdir(mode=0o700)
    original_modes = {path: stat.S_IMODE(path.stat().st_mode)
                      for path in [source, *source.rglob("*")]}
    destination = tmp_path / "uploaded"
    calls = []
    async def remote(phase, slug, version, upload_manifest=None, **kwargs):
        calls.append(phase)
        if phase == "prepare":
            return {"stage": f"/srv/fake-deploy/vococo-staging/{slug}-{version}"}
        if phase == "activate":
            return {"activation": "succeeded", "backup_path": f"/srv/fake-deploy/vococo-backups/{slug}/{version}"}
        return {"state": {}}
    async def command(argv, stdin=None):
        calls.append("upload")
        # 只替换 SSH 传输；权限参数仍由系统 rsync 实际解析和执行。
        local = list(argv)
        local.remove("--rsync-path=sudo -n rsync")
        transport = local.index("-e")
        del local[transport:transport + 2]
        local[-1] = str(destination) + "/"
        result = subprocess.run(local, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout
    async def verify(url):
        return {"http_verification": "verified"}
    monkeypatch.setattr(kimmy, "_remote", remote)
    monkeypatch.setattr(kimmy, "_command", command)
    monkeypatch.setattr(kimmy, "_verify", verify)
    result = asyncio.run(kimmy.publish(source, "sample", publishing._manifest(source)))
    assert result["activation"] == "succeeded"
    assert calls == ["inspect", "prepare", "upload", "activate"]
    assert publishing._manifest(destination) == publishing._manifest(source)
    for path in [destination, *destination.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)
    assert all(stat.S_IMODE(path.stat().st_mode) == mode for path, mode in original_modes.items())


def test_kimmy_denied_does_not_connect(isolated, params, monkeypatch):
    async def deny(*args): return False
    async def remote(*args, **kwargs): pytest.fail("审批拒绝不能连接SSH")
    monkeypatch.setattr(publishing.danger, "require_approval", deny)
    monkeypatch.setattr(kimmy, "_remote", remote)
    _, receipt = run_tool(params)
    assert receipt["activation"] == "denied"


def test_kimmy_unknown_activation_does_not_register_or_claim_failure_safe_to_repeat(isolated, params, monkeypatch):
    from vococo.memory import library
    async def approve(*args): return True
    async def remote(phase, slug, version, upload_manifest=None, **kwargs):
        if phase == "prepare": return {"stage": f"/srv/fake-deploy/vococo-staging/{slug}-{version}"}
        if phase == "activate": raise OSError("SSH lost")
        return {"state": {}}
    async def command(*args): return ""
    monkeypatch.setattr(publishing.danger, "require_approval", approve)
    monkeypatch.setattr(kimmy, "_remote", remote)
    monkeypatch.setattr(kimmy, "_command", command)
    text, receipt = run_tool(params)
    assert receipt["activation"] == "unknown"
    assert "不要重新发布" in text
    assert not library.list_items()


@pytest.mark.parametrize("status", [200, 503])
def test_http_verification_never_follows_redirects(monkeypatch, status):
    import aiohttp
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session(Response):
        def get(self, url, **kwargs):
            assert url == "https://pub.example.com/sample/"
            assert kwargs["allow_redirects"] is False
            response = Response()
            response.status = status
            return response
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    result = asyncio.run(kimmy._verify("https://pub.example.com/sample/"))
    assert result["http_verification"] == ("verified" if status == 200 else "pending")


def test_http_timeout_is_pending_not_activation_failure(monkeypatch):
    import aiohttp
    def fail(): raise TimeoutError("timeout")
    monkeypatch.setattr(aiohttp, "ClientSession", fail)
    assert asyncio.run(kimmy._verify("https://pub.example.com/sample/"))["http_verification"] == "pending"


@pytest.fixture
def remote_tree(isolated, monkeypatch, capsys):
    import subprocess
    import sys
    deploy = isolated / "deploy"
    deploy.mkdir()
    content = b"audited-deploy-script"
    (deploy / "deploy.sh").write_bytes(content)
    (deploy / "config").write_text("DOMAIN=pub.example.com\nURL_MODE=path\nSSL_CERT=/etc/letsencrypt/live/example.com/fullchain.pem\nSSL_KEY=/etc/letsencrypt/live/example.com/privkey.pem\n")
    nginx = isolated / "nginx"
    nginx.write_text("nginx")
    live_root, snippets = isolated / "www", isolated / "snippets"
    slug, version = "sample", "a" * 32
    settings = kimmy.kimmy_target()
    settings.update(deploy_root=str(deploy), web_root=str(live_root), snippet_root=str(snippets),
                    script_sha256=hashlib.sha256(content).hexdigest())
    script = kimmy._REMOTE.replace('Path("/usr/sbin/nginx")', f"Path({str(nginx)!r})")
    deployed = []
    git_status = [""]
    def subprocess_run(argv, **kwargs):
        if argv[0] == "git":
            return SimpleNamespace(returncode=0, stdout=git_status[0], stderr="")
        if argv in (["nginx", "-t"], ["systemctl", "reload", "nginx"]):
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        assert argv == ["bash", str(deploy / "deploy.sh"), slug, "static"]
        deployed.append(argv)
        destination = live_root / slug
        if destination.exists(): shutil.rmtree(destination)
        shutil.copytree(deploy / "incoming" / slug, destination)
        snippets.mkdir(parents=True, exist_ok=True)
        (snippets / (slug + ".conf")).write_text("static nginx")
        return SimpleNamespace(returncode=0, stdout="deployed", stderr="")
    monkeypatch.setattr(subprocess, "run", subprocess_run)
    def execute(phase, upload_manifest=None, operation_version=None):
        monkeypatch.setattr(sys, "argv", ["python", phase, slug, operation_version or version, json.dumps(settings), json.dumps(upload_manifest)])
        namespace = {}
        try:
            exec(compile(script, "isolated_remote", "exec"), namespace)
        finally:
            if "lock" in namespace:
                namespace["lock"].close()
        return json.loads(capsys.readouterr().out)
    return SimpleNamespace(root=deploy, live=live_root / slug, snippet=snippets / (slug + ".conf"),
        incoming=deploy / "incoming" / slug, stage=deploy / "vococo-staging" / (slug + "-" + version),
        execute=execute, deployed=deployed, git_status=git_status, version=version)


def write_site(directory, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(text)


def test_remote_new_publish_and_backup_then_known_updates(remote_tree):
    tree = remote_tree
    assert tree.execute("inspect")["state"]["live"] is None
    manifest = {"index.html": hashlib.sha256(b"new").hexdigest()}
    tree.execute("prepare", manifest)
    write_site(tree.stage / "site", "new")
    result = tree.execute("activate")
    assert result["activation"] == "succeeded"
    assert tree.live.joinpath("index.html").read_text() == "new"
    assert tree.execute("inspect")["state"]["live"] == manifest
    assert len(tree.deployed) == 1


def controlled_remote(tree):
    manifest = {'index.html': hashlib.sha256(b'controlled').hexdigest()}
    tree.execute('prepare', manifest)
    write_site(tree.stage / 'site', 'controlled')
    assert tree.execute('activate')['activation'] == 'succeeded'


def test_remote_unpublish_moves_live_incoming_nginx_and_is_idempotent(remote_tree):
    tree = remote_tree
    controlled_remote(tree)
    token = 'b' * 32
    result = tree.execute('unpublish', operation_version=token)
    assert result['deactivation'] == 'succeeded'
    backup = Path(result['backup_path'])
    assert not tree.live.exists() and not tree.incoming.exists() and not tree.snippet.exists()
    assert (backup / 'live/index.html').read_text() == 'controlled'
    assert (backup / 'nginx.conf').read_text() == 'static nginx'
    assert tree.execute('unpublish', operation_version='c' * 32) == result
    assert tree.execute('inspect')['state'] == {'incoming': None, 'live': None, 'snippet': None}


@pytest.mark.parametrize('tamper', ['', 'record', 'backup', 'recreated'])
def test_remote_offline_receipt_repairs_failed_publish_ledger_only_when_proven(remote_tree, monkeypatch, tamper):
    import os
    tree = remote_tree
    controlled_remote(tree)
    record = tree.root / 'vococo-state/sample.json'
    original_replace = os.replace
    fail = True
    def replace(source, destination):
        if fail and Path(destination) == record:
            raise OSError('模拟下线后发布账本写失败')
        return original_replace(source, destination)
    monkeypatch.setattr(os, 'replace', replace)
    result = tree.execute('unpublish', operation_version='b' * 32)
    assert result['deactivation'] == 'unknown'
    assert not tree.live.exists()
    offline = json.loads((tree.root / 'vococo-offline/sample.json').read_text())
    assert offline['deactivation'] == 'succeeded'
    assert json.loads(record.read_text())['live'] is not None
    fail = False
    if tamper == 'record':
        record.write_text(json.dumps({'incoming': None, 'live': {'foreign': 'hash'}, 'snippet': None}))
    elif tamper == 'backup':
        (Path(offline['backup_path']) / 'live/index.html').write_text('changed backup')
    elif tamper == 'recreated':
        write_site(tree.live, 'unregistered live')
    retried = tree.execute('unpublish', operation_version='c' * 32)
    if tamper:
        assert retried['deactivation'] != 'succeeded'
        assert 'error' in retried
    else:
        assert retried == offline
        assert json.loads(record.read_text()) == {'incoming': None, 'live': None, 'snippet': None}
        assert tree.execute('inspect')['state']['live'] is None
        assert not (tree.root / ('vococo-backups/sample/' + 'c' * 32)).exists()


def test_remote_republish_invalidates_old_offline_receipt_even_for_identical_content(remote_tree):
    tree = remote_tree
    controlled_remote(tree)
    assert tree.execute('unpublish', operation_version='b' * 32)['deactivation'] == 'succeeded'
    version = 'd' * 32
    manifest = {'index.html': hashlib.sha256(b'controlled').hexdigest()}
    tree.execute('prepare', manifest, operation_version=version)
    write_site(tree.root / 'vococo-staging' / ('sample-' + version) / 'site', 'controlled')
    assert tree.execute('activate', operation_version=version)['activation'] == 'succeeded'
    assert not (tree.root / 'vococo-offline/sample.json').exists()
    shutil.rmtree(tree.live)
    shutil.rmtree(tree.incoming)
    tree.snippet.unlink()
    assert '未登记改动' in tree.execute('inspect')['error']
    assert tree.execute('unpublish', operation_version='e' * 32)['deactivation'] != 'succeeded'


@pytest.mark.parametrize('change', ['content', 'nginx', 'unmanaged', 'symlink'])
def test_remote_unpublish_refuses_unknown_changes(remote_tree, change):
    tree = remote_tree
    controlled_remote(tree)
    if change == 'content':
        (tree.live / 'index.html').write_text('unregistered')
    elif change == 'nginx':
        tree.snippet.write_text('unregistered')
    elif change == 'unmanaged':
        (tree.root / 'vococo-state/sample.json').unlink()
    else:
        tree.snippet.rename(tree.snippet.with_suffix('.original'))
        tree.snippet.symlink_to(tree.snippet.with_suffix('.original'))
    result = tree.execute('unpublish', operation_version='b' * 32)
    assert result['deactivation'] != 'succeeded'
    assert tree.live.is_dir() and tree.snippet.exists()


def test_remote_unpublish_ignores_attacker_fixed_temporary_symlinks(remote_tree):
    tree = remote_tree
    controlled_remote(tree)
    victim = tree.root / 'victim'
    victim.write_text('must stay')
    (tree.root / 'vococo-state/sample.next').symlink_to(victim)
    (tree.root / 'vococo-offline').mkdir()
    (tree.root / 'vococo-offline/sample.next').symlink_to(victim)
    assert tree.execute('unpublish', operation_version='b' * 32)['deactivation'] == 'succeeded'
    assert victim.read_text() == 'must stay'


def test_remote_first_takeover_refuses_divergent_server_files(remote_tree):
    tree = remote_tree
    write_site(tree.incoming, "local old")
    write_site(tree.live, "server newer")
    result = tree.execute("inspect")
    assert "incoming 与在线文件不一致" in result["error"]
    assert not tree.deployed


def test_remote_matching_first_takeover_backs_up_before_original_script(remote_tree):
    tree = remote_tree
    write_site(tree.incoming, "old")
    write_site(tree.live, "old")
    manifest = {"index.html": hashlib.sha256(b"new").hexdigest()}
    tree.execute("prepare", manifest)
    write_site(tree.stage / "site", "new")
    result = tree.execute("activate")
    assert result["activation"] == "succeeded"
    backup = Path(result["backup_path"])
    assert (backup / "incoming" / "index.html").read_text() == "old"
    assert (backup / "live" / "index.html").read_text() == "old"
    assert (backup / "incoming-original" / "index.html").read_text() == "old"


def test_remote_git_untracked_changes_block_before_upload(remote_tree):
    tree = remote_tree
    write_site(tree.incoming, "old")
    write_site(tree.live, "old")
    (tree.incoming / ".git").mkdir()
    tree.git_status[0] = "?? untracked.js"
    assert "未提交或未跟踪" in tree.execute("inspect")["error"]
    assert not tree.deployed


def test_remote_changes_during_upload_do_not_overwrite_live(remote_tree):
    tree = remote_tree
    write_site(tree.incoming, "old")
    write_site(tree.live, "old")
    manifest = {"index.html": hashlib.sha256(b"new").hexdigest()}
    tree.execute("prepare", manifest)
    write_site(tree.stage / "site", "new")
    tree.live.joinpath("index.html").write_text("unrecorded")
    result = tree.execute("activate")
    assert result["activation"] == "unknown"
    assert not tree.deployed
    assert tree.live.joinpath("index.html").read_text() == "unrecorded"


def test_remote_uploaded_snapshot_must_match_approval(remote_tree):
    tree = remote_tree
    tree.execute("prepare", {"index.html": hashlib.sha256(b"approved").hexdigest()})
    write_site(tree.stage / "site", "altered")
    assert "与已审批快照不一致" in tree.execute("activate")["error"]
    assert not tree.deployed


@pytest.mark.parametrize("change", ["script", "config"])
def test_remote_changed_script_or_config_is_not_executed(remote_tree, change):
    tree = remote_tree
    path = tree.root / ("deploy.sh" if change == "script" else "config")
    path.write_text("altered")
    assert tree.execute("inspect")["activation"] == "failed"
    assert not tree.deployed
