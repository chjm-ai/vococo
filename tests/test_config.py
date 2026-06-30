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
