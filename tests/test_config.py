"""配置解析:skill 白名单 / 会话键统一 / 布尔解析。"""
from __future__ import annotations


def test_parse_skills():
    from claude_hermes import config

    assert config._parse_skills("") is None        # 留空 = 全量(向后兼容)
    assert config._parse_skills("   ") is None
    assert config._parse_skills("all") == "all"
    assert config._parse_skills("ALL") == "all"
    assert config._parse_skills("a,b c") == ["a", "b", "c"]
    assert config._parse_skills("monthly-planner") == ["monthly-planner"]


def test_parse_bool():
    from claude_hermes import config

    assert config._parse_bool("", True) is True       # 空用默认
    assert config._parse_bool("", False) is False
    assert config._parse_bool("0", True) is False
    assert config._parse_bool("no", True) is False
    assert config._parse_bool("off", True) is False
    assert config._parse_bool("1", False) is True
    assert config._parse_bool("yes", False) is True


def test_resolve_session_key_unified(monkeypatch):
    from claude_hermes import config

    monkeypatch.setattr(config, "UNIFY_SESSIONS", True)
    monkeypatch.setattr(config, "SESSION_KEY", "main")
    # 不同入口都归到同一会话 → 跨入口连续
    assert config.resolve_session_key("telegram", 123) == "main"
    assert config.resolve_session_key("cli", "local") == "main"
    assert config.resolve_session_key("feishu", "abc") == "main"


def test_resolve_session_key_isolated(monkeypatch):
    from claude_hermes import config

    monkeypatch.setattr(config, "UNIFY_SESSIONS", False)
    assert config.resolve_session_key("telegram", 123) == "telegram:123"
    assert config.resolve_session_key("cli", "local") == "cli:local"


def test_project_cwd_for(isolated, tmp_path):
    from claude_hermes import config
    from claude_hermes.memory import session_store

    d = tmp_path / "repo"
    d.mkdir()
    h = session_store.upsert_project(str(d))["hash"]
    # 项目会话:web:p<hash>:<conv> → 该文件夹
    assert config.project_cwd_for(f"web:p{h}:c1") == str(d.resolve())
    # 非项目会话都回落到 None(进程默认目录)
    assert config.project_cwd_for("web:legacyconv") is None
    assert config.project_cwd_for("main") is None
    assert config.project_cwd_for("tg:-100") is None
    # 未知哈希 → None
    assert config.project_cwd_for("web:pdeadbeef00:c1") is None
