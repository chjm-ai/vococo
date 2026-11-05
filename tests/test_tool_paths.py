"""本机工具路径的离线回归；不下载素材、不读真实认证、不调用模型。"""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from vococo.instance_config import local_path

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "vococo/plugin/skills/video-assets/scripts/manage.py"
ROUTING = ROOT / "scripts/verify_session_routing.py"


@pytest.fixture
def private(tmp_path, monkeypatch):
    path = tmp_path / "instance.json"
    values = {"version": 1, "video_assets": {"library_dir": str(tmp_path / "library")},
              "session_routing": {"env_file": str(tmp_path / "test.env")}}
    path.write_text(json.dumps(values))
    monkeypatch.setenv("VOCOCO_INSTANCE_CONFIG", str(path))
    for name in ("VOCOCO_VIDEO_ASSETS_DIR", "VOCOCO_SESSION_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    return path, values


def test_paths_are_optional_until_tool_requires_them(private, tmp_path):
    path, _ = private
    path.unlink()
    assert local_path("video_assets", "library_dir", root=tmp_path, env="VOCOCO_VIDEO_ASSETS_DIR") is None
    video = runpy.run_path(str(VIDEO))
    with pytest.raises(ValueError, match="请配置"):
        video["configure"]()
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize("value", [None, "", " ", "relative", [], 123, "/tmp/bad\npath", "/tmp/\x00bad"])
def test_invalid_path_fails_before_creating_library(private, monkeypatch, value):
    path, values = private
    values["video_assets"]["library_dir"] = value
    path.write_text(json.dumps(values))
    video = runpy.run_path(str(VIDEO))
    with pytest.raises(ValueError, match="绝对路径"):
        video["configure"]()
    assert not (path.parent / "library").exists()
    monkeypatch.setenv("VOCOCO_VIDEO_ASSETS_DIR", "")
    with pytest.raises(ValueError, match="绝对路径"):
        video["configure"]()


def test_environment_overrides_config_without_loading_it(private, monkeypatch, tmp_path):
    private[0].write_text("not json")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VOCOCO_VIDEO_ASSETS_DIR", "~/alternate")
    assert local_path("video_assets", "library_dir", root=ROOT, env="VOCOCO_VIDEO_ASSETS_DIR") == tmp_path / "alternate"
    monkeypatch.delenv("VOCOCO_VIDEO_ASSETS_DIR")
    with pytest.raises(ValueError, match="有效 JSON"):
        local_path("video_assets", "library_dir", root=ROOT, env="VOCOCO_VIDEO_ASSETS_DIR")
    monkeypatch.setenv("VOCOCO_INSTANCE_CONFIG", "relative.json")
    with pytest.raises(ValueError, match="VOCOCO_INSTANCE_CONFIG"):
        local_path("video_assets", "library_dir", root=ROOT, env="VOCOCO_VIDEO_ASSETS_DIR")


def test_video_reads_existing_index_and_writes_only_configured_library(private, tmp_path):
    library = Path(private[1]["video_assets"]["library_dir"])
    library.mkdir()
    index = library / "index.json"
    old = [{"id": "v0009", "file": "clips/existing.mp4", "notes": "原有记录"}]
    index.write_text(json.dumps(old))
    video = runpy.run_path(str(VIDEO))
    video["configure"]()
    scope = video["configure"].__globals__
    assert scope["LIBRARY"] == library and scope["CLIPS"] == library / "clips"
    assert video["load_index"]() == old
    video["save_index"](old + [{"id": "v0010"}])
    assert json.loads(index.read_text())[0] == old[0]
    assert not (tmp_path / "Media").exists()


def test_routing_loads_only_configured_fallback_without_importing_runtime(private, monkeypatch, tmp_path):
    env_file = Path(private[1]["session_routing"]["env_file"])
    env_file.write_text("VOCOCO_PATH_TEST=fixture\nVOCOCO_KEEP_TEST=fixture\n")
    monkeypatch.delenv("VOCOCO_PATH_TEST", raising=False)
    monkeypatch.setenv("VOCOCO_KEEP_TEST", "existing")
    routing = runpy.run_path(str(ROUTING))
    try:
        routing["load_runtime_env"](tmp_path / "worktree")
        assert os.environ["VOCOCO_PATH_TEST"] == "fixture"
        assert os.environ["VOCOCO_KEEP_TEST"] == "existing"
    finally:
        os.environ.pop("VOCOCO_PATH_TEST", None)
    # 有本安装 .env 时，不碰缺失的外部认证文件。
    (tmp_path / ".env").write_text("VOCOCO_PATH_TEST=local\n")
    env_file.unlink()
    routing["load_runtime_env"](tmp_path)
    with pytest.raises(ValueError, match="认证文件不存在"):
        routing["load_runtime_env"](tmp_path / "worktree")
    monkeypatch.setenv("VOCOCO_SESSION_ENV_FILE", str(tmp_path / ".env"))
    try:
        routing["load_runtime_env"](tmp_path / "worktree")
        assert os.environ["VOCOCO_PATH_TEST"] == "local"
    finally:
        os.environ.pop("VOCOCO_PATH_TEST", None)


def test_routing_without_fallback_keeps_existing_process_auth(private, tmp_path):
    private[0].unlink()
    routing = runpy.run_path(str(ROUTING))
    routing["load_runtime_env"](tmp_path)


@pytest.mark.parametrize("script,args", [(VIDEO, ["--help"]), (VIDEO, ["list"]),
                                         (ROUTING, ["--help"])])
def test_cli_from_other_directory_needs_no_runtime_login(private, tmp_path, script, args):
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    private[0].unlink()
    result = subprocess.run([sys.executable, "-B", str(script), *args], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    expected = 0 if "--help" in args else (2 if script == VIDEO else 1)
    assert result.returncode == expected, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "library").exists()


def test_default_instance_path_belongs_to_install_and_does_not_import_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("VOCOCO_INSTANCE_CONFIG", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/instance_config.json").write_text(json.dumps(
        {"version": 1, "video_assets": {"library_dir": str(tmp_path / "chosen")}}))
    assert local_path("video_assets", "library_dir", root=tmp_path, env="VOCOCO_VIDEO_ASSETS_DIR") == tmp_path / "chosen"
    code = ("import runpy, sys; "
            f"runpy.run_path({str(VIDEO)!r}); runpy.run_path({str(ROUTING)!r}); "
            "assert 'vococo.config' not in sys.modules")
    subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, check=True, capture_output=True, timeout=30)


def test_migration_extracts_old_paths_without_executing_code(private, monkeypatch, tmp_path):
    migration = runpy.run_path(str(ROOT / "scripts/migrate_local_preferences.py"))
    sources = {"manage.py": 'LIBRARY = Path.home() / "Media" / "video-assets"\nraise RuntimeError()',
               "ppt_master_check.py": 'root = os.environ.get("VOCOCO_PPT_MASTER_DIR", "~/shared/ppt")',
               "verify_session_routing.py": 'load_dotenv(Path.home() / "custom/vococo/.env", override=False)'}
    extract = migration["extract_tool_paths"]
    monkeypatch.setitem(extract.__globals__, "source", lambda ref, path: sources[Path(path).name])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    additions = extract("old-ref")
    assert additions == {"video_assets": {"library_dir": str(tmp_path / "Media/video-assets")},
                         "ppt_master": {"install_dir": str(tmp_path / "shared/ppt")},
                         "session_routing": {"env_file": str(tmp_path / "custom/vococo/.env")}}
    path = tmp_path / "target.json"
    path.write_text('{"version":1,"unrelated":{"keep":true}}')
    before = path.read_bytes()
    backup = migration["append_config"](path, additions, apply=True)
    assert backup.read_bytes() == before
    assert json.loads(path.read_text()) == {"version": 1, "unrelated": {"keep": True}, **additions}
    assert path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    assert migration["append_config"](path, additions, apply=True) is None
    sources["verify_session_routing.py"] = 'load_dotenv(execute_untrusted())'
    with pytest.raises(ValueError):
        extract("old-ref")
