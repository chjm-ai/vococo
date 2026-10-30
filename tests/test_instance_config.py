"""实例数据读取、旧版本迁移和配置权限保护。"""
import importlib.util
import json
import stat
from pathlib import Path

import pytest

from vococo import config, instance_config
from vococo.gateway import brain_routes
from vococo.memory import agents
from vococo.tools import danger


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path / "instance_config.json"


def test_missing_and_live_config(private_config):
    assert instance_config.section("brain") == {}
    assert instance_config.strings("preferences", "memory_sections") == []
    assert instance_config.text("brain", "agent_id") == ""
    private_config.write_text(json.dumps({"version": 1, "brain": {"agent_id": "123456abcd"}}))
    assert brain_routes.bridge_agent_id() == "123456abcd"
    private_config.write_text(json.dumps({"version": 1, "brain": {"agent_id": "abcdef1234"}}))
    assert brain_routes.bridge_agent_id() == "abcdef1234"


@pytest.mark.parametrize("raw", ["{", "[]", '{"version": 2}', '{"version": 1, "brain": []}', "x" * 256001])
def test_invalid_config_rejected(private_config, raw):
    private_config.write_text(raw)
    with pytest.raises(ValueError):
        instance_config.section("brain")


def test_environment_binding_takes_precedence(private_config, monkeypatch):
    private_config.write_text('{"version": 1, "brain": {"agent_id": "123456abcd"}}')
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", "abcdef1234")
    assert brain_routes.bridge_agent_id() == "abcdef1234"
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", "")
    with pytest.raises(ValueError):
        brain_routes.bridge_agent_id()


def test_custom_preferences_are_data(private_config):
    private_config.write_text(json.dumps({"version": 1, "preferences": {"memory_sections": ["自定义"]}}))
    assert agents.default_memory_sections() == ["自定义"]
    private_config.write_text('{"version":1,"preferences":{"memory_sections":"wrong"}}')
    with pytest.raises(ValueError):
        agents.default_memory_sections()


def test_instance_config_changes_need_approval(private_config):
    from vococo.core import permissions

    assert danger.PERMISSION_CONFIG_REASON in permissions.KEEP_ASKING
    for tool in ("Write", "Edit"):
        assert danger.classify(tool, {"file_path": str(private_config)}, str(private_config.parent)) == (
            "escalate", danger.PERMISSION_CONFIG_REASON, True)
    assert danger.classify("Bash", {"command": "python3 -c 'write(instance_config.json)'"}, str(private_config.parent))[0] == "escalate"
    assert danger.classify("Read", {"file_path": str(private_config)}, str(private_config.parent))[0] == "allow"


def _migration():
    path = Path(__file__).parents[1] / "scripts" / "migrate_instance_config.py"
    spec = importlib.util.spec_from_file_location("migrate_instance_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_preserves_old_values_without_executing_sources(monkeypatch, tmp_path):
    migration = _migration()
    sources = {
        "brain_routes.py": 'AGENT_ID = "123456abcd"\nraise RuntimeError("不要执行")',
        "brain_mcp.py": 'allowed_origins=["https://mcp.example.com"]',
        "preview.js": 'new URL(share.path,"https://share.example.com")',
        "publishing_kimmy.py": '''_DEPLOY_SHA256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ssh = ["ConnectTimeout=10", "test-publisher"]
url = f"https://pub.example.com/{slug}/"
_REMOTE = r\'\'\'
root = Path("/srv/deploy")
incoming, live = root / "incoming" / slug, Path("/srv/www") / slug
snippet = Path("/srv/snippets") / (slug + ".conf")
expected = {"DOMAIN=pub.example.com", "URL_MODE=path", "SSL_CERT=/etc/letsencrypt/live/pub.example.com/fullchain.pem", "SSL_KEY=/etc/letsencrypt/live/pub.example.com/privkey.pem"}
\'\'\'
''',
        "prompt.py": '- 回复使用 Markdown；测试排版\n\n=== 记忆工具\n=== 执行方式(重要)===\n测试派发偏好\n- 以下这条专指 Agent 子代理工具:',
        "agents.py": 'DEFAULT_MEMORY_SECTIONS = ["测试"]\nMEMORY_RULES = "目录：{dir}"\nDECISION_RULES = "先询问"',
        "agent.py": '_MCP_TASK_KEYWORDS = {"fake-tool": ["测试"]}',
        "config.py": 'value = get("PEOPLE_PROFILES_OBSIDIAN_FOLDERS", "测试目录,其他目录")',
    }
    monkeypatch.setattr(migration, "source", lambda ref, path: sources[Path(path).name])
    data = migration.extract("test-old-ref")
    assert data["brain"] == {"agent_id": "123456abcd", "origin": "https://mcp.example.com"}
    assert data["preferences"]["people_folders"] == ["测试目录", "其他目录"]
    assert "测试派发偏好" in data["preferences"]["persona"]
    assert data["mcp"]["task_keywords"] == {"fake-tool": ["测试"]}
    assert data["publishing"]["legacy_kimmy_fingerprint"] == migration.fingerprint(data["publishing"]["kimmy"])
    destination = tmp_path / "private" / "instance_config.json"
    migration.write_new(destination, data)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(destination.read_text()) == data
    with pytest.raises(FileExistsError):
        migration.write_new(destination, {"version": 1})
    assert json.loads(destination.read_text()) == data
    link = tmp_path / "alias"
    link.symlink_to(destination.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="软链接"):
        migration.write_new(link / "other.json", data)
