"""配置解析:skill 白名单 / 会话键统一 / 布尔解析。"""
from __future__ import annotations


def test_parse_skills():
    from vococo import config

    assert config._parse_skills("") is None        # 留空 = 全量(向后兼容)
    assert config._parse_skills("   ") is None
    assert config._parse_skills("all") == "all"
    assert config._parse_skills("ALL") == "all"
    assert config._parse_skills("a,b c") == ["a", "b", "c"]
    assert config._parse_skills("monthly-planner") == ["monthly-planner"]


def test_parse_bool():
    from vococo import config

    assert config._parse_bool("", True) is True       # 空用默认
    assert config._parse_bool("", False) is False
    assert config._parse_bool("0", True) is False
    assert config._parse_bool("no", True) is False
    assert config._parse_bool("off", True) is False
    assert config._parse_bool("1", False) is True
    assert config._parse_bool("yes", False) is True


def test_project_hash_from_key():
    from vococo import config

    # 正式项目会话(改名前的存量形态)
    assert config.project_hash_from_key("web:pabc123def:s1") == "abc123def"
    # 前端草稿会话(local- 前缀,改名后新建会话的形态——不识别会导致不建 worktree)
    assert config.project_hash_from_key("web:local-pabc123def:s1") == "abc123def"
    # 无项目的纯草稿 / 主会话 / CLI / task 前缀:都不是项目会话
    assert config.project_hash_from_key("web:local-abc123") is None
    assert config.project_hash_from_key("web:main") is None
    assert config.project_hash_from_key("cli") is None
    assert config.project_hash_from_key("task:abc") is None


def test_resolve_session_key_unified(monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "UNIFY_SESSIONS", True)
    monkeypatch.setattr(config, "SESSION_KEY", "main")
    # 不同入口都归到同一会话 → 跨入口连续
    assert config.resolve_session_key("cli", "local") == "main"
    assert config.resolve_session_key("voice", "abc") == "main"


def test_resolve_session_key_isolated(monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "UNIFY_SESSIONS", False)
    assert config.resolve_session_key("cli", "local") == "cli:local"
    assert config.resolve_session_key("voice", "abc") == "voice:abc"


def test_resolve_session_key_passes_through_voice_prefixes():
    from vococo import config

    # voice-chat:/task: 已经是完整 key,web 端不该再套一层 "web:" 前缀,否则语音
    # 主会话/统一后台任务会话没法跟 index.html 现成的 openConv() 对上号。
    assert config.resolve_session_key("web", "voice-chat:main") == "voice-chat:main"
    assert config.resolve_session_key("web", "task:abc123") == "task:abc123"
    # 普通项目哈希形态的 conv_id 不受影响
    assert config.resolve_session_key("web", "p1234:5") == "web:p1234:5"


def test_execution_project_root_falls_back_to_default(isolated, monkeypatch, tmp_path):
    from vococo import config
    from vococo.memory import session_store

    default = tmp_path / "default"
    monkeypatch.setattr(config, "ROOT_DIR", default)
    project = tmp_path / "project"
    project.mkdir()
    h = session_store.upsert_project(str(project))["hash"]

    assert config.execution_project_root_for(f"web:p{h}:c1") == str(project.resolve())
    assert config.execution_project_root_for("web:unmatched") == str(default)
    assert config.execution_project_root_for() == str(default)
    assert config.resolve_execution_root(session_key=f"web:p{h}:c1") == str(project.resolve())
    assert config.resolve_execution_root(cwd="/tmp/explicit") == "/tmp/explicit"


def test_project_cwd_for(isolated, tmp_path):
    from vococo import config
    from vococo.memory import session_store

    d = tmp_path / "repo"
    d.mkdir()
    h = session_store.upsert_project(str(d))["hash"]
    # 项目会话:web:p<hash>:<conv> → 该文件夹
    assert config.project_cwd_for(f"web:p{h}:c1") == str(d.resolve())
    # 非项目会话都回落到 None(进程默认目录)
    assert config.project_cwd_for("web:legacyconv") is None
    assert config.project_cwd_for("main") is None
    assert config.project_cwd_for("cli") is None
    # 未知哈希 → None
    assert config.project_cwd_for("web:pdeadbeef00:c1") is None
