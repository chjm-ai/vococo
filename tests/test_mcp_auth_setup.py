"""全部凭据、服务文件、下载物均使用临时目录，不访问生产配置/DB。"""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_mcp_auth.py"
spec = importlib.util.spec_from_file_location("setup_mcp_auth", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
HASH = "$2y$12$" + "a" * 53
TOKEN = "test-upstream-token-" + "a" * 32


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(setup, "check_service", lambda: None)
    monkeypatch.setattr(setup, "password_hash", lambda: HASH)
    monkeypatch.setattr(setup, "hidden_input", lambda prompt: TOKEN)
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup, "fetch_release", lambda: pytest.fail("禁止真实GitHub请求"))
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: pytest.fail("禁止真实下载"))


@pytest.fixture
def root(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path.resolve() / "private-auth"


def asset_release(data=b"fake binary fixture; never execute"):
    return {"tag_name": setup.VERSION, "assets": [{
        "name": setup.ASSET, "browser_download_url": setup.ASSET_URL,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }]}


def test_prepare_private_fixed_configuration_and_secret_free_plist(root):
    assert setup.prepare(root)
    setup.verify_prepared(root)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "data").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir() if p.is_file())
    env = (root / "proxy.env").read_text()
    for setting in ["EXTERNAL_URL=https://mcp.chjm.cc\n", "NO_AUTO_TLS=true\n",
                    "LISTEN=127.0.0.1:8853\n", "REPOSITORY_BACKEND=local\n",
                    "PROXY_FORWARD_AUTHORIZATION=false\n", str(root / "data"), HASH, TOKEN]:
        assert setting in env
    assert "PASSWORD=" not in env and "JWT_PRIVATE_KEY=" not in env
    plist = plistlib.loads((root / "cc.chjm.mcp-auth.plist").read_bytes())
    assert plist["ProgramArguments"][:3] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin:/usr/sbin:/sbin"]
    assert plist["Umask"] == 0o077
    assert "EnvironmentVariables" not in plist
    for name in ["cc.chjm.mcp-auth.plist", "run-proxy.sh", "prepared.json"]:
        assert HASH not in (root / name).read_text()
        assert TOKEN not in (root / name).read_text()
    assert not (root / setup.BINARY).exists()
    assert list((root / "data").iterdir()) == []


def test_repeat_does_not_prompt_or_rotate_persistent_data(root, monkeypatch):
    setup.prepare(root)
    for name in ["secret", "private_key.pem", "oauth.db"]:
        setup.write_private(root / "data" / name, b"existing opaque state")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(setup, "hidden_input", lambda _: pytest.fail("重复执行不可重新要凭据"))
    monkeypatch.setattr(setup, "password_hash", lambda: pytest.fail("不可重新hash"))
    assert not setup.prepare(root)
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("changed", ["proxy.env", "run-proxy.sh", "cc.chjm.mcp-auth.plist"])
def test_changed_existing_config_is_never_overwritten(root, changed):
    setup.prepare(root)
    target = root / changed
    target.write_bytes(b"locally modified")
    with pytest.raises(setup.SetupError, match="拒绝覆盖"):
        setup.prepare(root)
    assert target.read_bytes() == b"locally modified"


def test_unknown_existing_service_or_partial_directory_not_overwritten(root, monkeypatch):
    root.mkdir(mode=0o700)
    target = root / "cc.chjm.mcp-auth.plist"
    target.write_bytes(b"existing service")
    monkeypatch.setattr(setup, "password_hash", lambda: pytest.fail("不可读取新口令"))
    with pytest.raises((OSError, setup.SetupError)):
        setup.prepare(root)
    assert target.read_bytes() == b"existing service"


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o400])
def test_unsafe_file_modes_refused_without_chmod(root, mode):
    setup.prepare(root)
    path = root / "proxy.env"
    path.chmod(mode)
    with pytest.raises(setup.SetupError):
        setup.verify_prepared(root)
    assert path.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("name", ["proxy.env", "prepared.json", "data"])
def test_symlink_existing_file_or_data_directory_refused(root, tmp_path, name):
    setup.prepare(root)
    target = root / name
    target.rename(tmp_path / "original")
    target.symlink_to(tmp_path / "original")
    with pytest.raises(setup.SetupError):
        setup.prepare(root)


def test_hardlinked_secret_refused(root):
    setup.prepare(root)
    os.link(root / "proxy.env", root / "alias")
    with pytest.raises(setup.SetupError):
        setup.verify_prepared(root)


def test_root_validation_rejects_repo_relative_symlink_and_public_parent(root, tmp_path):
    for value in ["relative", str(setup.REPO / "credentials")]:
        with pytest.raises(setup.SetupError):
            setup.checked_root(value)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(setup.SetupError):
        setup.checked_root(str(alias / "private"))
    tmp_path.chmod(0o777)
    with pytest.raises(setup.SetupError):
        setup.checked_root(str(root))


def test_hidden_input_requires_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(setup.SetupError, match="交互终端"):
        # 调用真实函数，而非自动隔离fixture的替代函数。
        real_hidden_input("不可回显")


real_hidden_input = setup.hidden_input
real_password_hash = setup.password_hash
real_check_service = setup.check_service


@pytest.mark.parametrize("password,confirmation", [
    ("short", "short"), ("a" * 73, "a" * 73), ("中" * 25, "中" * 25),
    ("a" * 16 + "\n", "a" * 16 + "\n"), ("a" * 16, "b" * 16),
])
def test_password_invalid_length_multibyte_newline_or_mismatch(monkeypatch, password, confirmation):
    inputs = iter([password, confirmation])
    monkeypatch.setattr(setup, "hidden_input", lambda _: next(inputs))
    with pytest.raises(setup.SetupError):
        real_password_hash()


@pytest.mark.parametrize("token", ["", "short", "x" * 32 + "\nInjected=1", "x" * 32 + " $(id)"])
def test_invalid_token_creates_no_directory(root, monkeypatch, token):
    monkeypatch.setattr(setup, "hidden_input", lambda _: token)
    with pytest.raises(setup.SetupError):
        setup.prepare(root)
    assert not root.exists()


def test_htpasswd_uses_stdin_never_argv_or_tool_error(monkeypatch):
    observed = {}
    def run(args, **kwargs):
        observed.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, "mcp:" + HASH + "\n\n", "")
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.htpasswd_hash("not-a-real-password") == HASH
    assert "not-a-real-password" not in " ".join(observed["args"])
    assert observed["input"] == "not-a-real-password\n"
    assert "-niBC" in observed["args"]
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "sensitive stdout", "sensitive stderr"))
    with pytest.raises(setup.SetupError) as error:
        setup.htpasswd_hash("not-a-real-password")
    assert "sensitive" not in str(error.value)


@pytest.mark.skipif(not Path("/usr/sbin/htpasswd").is_file(), reason="无系统htpasswd")
def test_real_system_htpasswd_bcrypt_and_verification(tmp_path):
    # 仅使用公开测试字符串；验证摘要真的由成熟bcrypt实现生成。
    hashed = setup.htpasswd_hash("public-test-password-123")
    assert setup.BCRYPT.fullmatch(hashed)
    password_file = tmp_path / "test-passwords"
    password_file.write_text("mcp:" + hashed + "\n")
    result = subprocess.run(["/usr/sbin/htpasswd", "-vi", str(password_file), "mcp"],
                            input="public-test-password-123\n", text=True, capture_output=True)
    assert result.returncode == 0


def test_loaded_service_refused_without_loading_or_reading_home(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(setup.SetupError, match="已加载"):
        real_check_service()
    assert calls == [["/bin/launchctl", "print", f"gui/{os.getuid()}/{setup.LABEL}"]]


@pytest.mark.parametrize("change", ["missing", "invalid", "url", "tag", "duplicate", "draft"])
def test_unverifiable_release_fails_closed(change):
    release = asset_release()
    asset = release["assets"][0]
    if change == "missing":
        asset.pop("digest")
    elif change == "invalid":
        asset["digest"] = "sha256:not-a-digest"
    elif change == "url":
        asset["browser_download_url"] = "https://example.com/untrusted"
    elif change == "tag":
        release["tag_name"] = "latest"
    elif change == "duplicate":
        release["assets"].append(asset)
    else:
        release["draft"] = True
    with pytest.raises(setup.SetupError):
        setup.release_asset(release)


class Response(io.BytesIO):
    def geturl(self):
        return "https://release-assets.githubusercontent.com/fake-test-asset"


def mock_download(monkeypatch, content, metadata=None):
    monkeypatch.setattr(setup, "fetch_release", lambda: metadata or asset_release(content))
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: Response(content))


def test_download_verified_inert_idempotent_and_preserves_oauth(root, monkeypatch):
    setup.prepare(root)
    content = b"fixture-not-executable"
    mock_download(monkeypatch, content)
    setup.write_private(root / "data" / "secret", b"persistent signing key")
    assert setup.download_verified(root)
    binary = root / setup.BINARY
    assert binary.read_bytes() == content
    assert binary.stat().st_mode & 0o777 == 0o600
    before = binary.stat().st_mtime_ns
    assert not setup.download_verified(root)
    assert binary.stat().st_mtime_ns == before
    assert (root / "data" / "secret").read_bytes() == b"persistent signing key"
    assert not list(root.glob(".download-*"))


def test_digest_mismatch_never_saves_or_executes_binary(root, monkeypatch):
    setup.prepare(root)
    mock_download(monkeypatch, b"tampered", asset_release(b"official"))
    with pytest.raises(setup.SetupError, match="SHA-256"):
        setup.download_verified(root)
    assert not (root / setup.BINARY).exists()
    assert not list(root.glob(".download-*"))


def test_missing_digest_fails_before_download(root, monkeypatch, capsys):
    setup.prepare(root)
    release = asset_release()
    release["assets"][0]["digest"] = None
    monkeypatch.setattr(setup, "fetch_release", lambda: release)
    assert setup.main(["download", "--private-dir", str(root)]) == 1
    assert "缺少 SHA-256 digest" in capsys.readouterr().err


def test_existing_different_binary_never_overwritten(root, monkeypatch):
    setup.prepare(root)
    mock_download(monkeypatch, b"official")
    setup.write_private(root / setup.BINARY, b"existing")
    with pytest.raises(setup.SetupError, match="拒绝覆盖"):
        setup.download_verified(root)
    assert (root / setup.BINARY).read_bytes() == b"existing"


def test_download_wrong_arch_refused(root, monkeypatch):
    setup.prepare(root)
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    with pytest.raises(setup.SetupError, match="x86_64"):
        setup.download_verified(root)


def test_download_size_and_empty_response_refused(monkeypatch):
    monkeypatch.setattr(setup, "MAX_BINARY", 2)
    for data in [b"", b"123"]:
        with pytest.raises(setup.SetupError):
            setup.copy_binary(io.BytesIO(data), io.BytesIO())


def test_cli_success_and_failure_do_not_log_secrets(root, capsys, monkeypatch):
    assert setup.main(["prepare", "--private-dir", str(root)]) == 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and HASH not in output.out + output.err
    monkeypatch.setattr(setup, "prepare", lambda _: (_ for _ in ()).throw(ValueError(TOKEN)))
    assert setup.main(["prepare", "--private-dir", str(root)]) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_runner_shell_quotes_paths_and_preserves_mcp_path(root, monkeypatch):
    root = root.with_name("auth 'quoted path")
    setup.prepare(root)
    # 不是下载物：人工测试桩仅验证shell/env/argv，不启动监听。
    stub = b'''#!/bin/sh
[ "$#" = 1 ] && [ "$1" = http://127.0.0.1:8852 ] || exit 11
[ "$LISTEN" = 127.0.0.1:8853 ] || exit 12
[ "$EXTERNAL_URL" = https://mcp.chjm.cc ] || exit 13
[ -z "${PASSWORD+x}" ] && [ -z "${JWT_PRIVATE_KEY+x}" ] || exit 14
[ "$PROXY_FORWARD_AUTHORIZATION" = false ] || exit 15
'''
    setup.write_private(root / setup.BINARY, stub)
    (root / setup.BINARY).chmod(0o700)
    plist = plistlib.loads((root / "cc.chjm.mcp-auth.plist").read_bytes())
    result = subprocess.run(plist["ProgramArguments"], cwd=root, capture_output=True,
                            env={"PASSWORD": "must-not-inherit", "JWT_PRIVATE_KEY": "bad-key"})
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
