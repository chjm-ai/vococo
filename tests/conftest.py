"""测试夹具。

- 兜底设一个假 OAUTH token:导入 vococo.config 会校验订阅令牌,本机有 .env
  会覆盖成真值(测试不连网,值无所谓);CI 无 .env 时用这个假值也能 import。
- isolated:把会话库与 AI_BRAIN 指到临时目录,并重置 memory/_db.py 的连接单例
  (session_store 及其兄弟模块 images/projects/worktrees/prefs/search 共用它),
  保证用例之间互不污染、也绝不碰真实的 ~/AI_BRAIN。
"""
from __future__ import annotations

import os

os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from vococo import config
    from vococo.memory import _db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AI_BRAIN_DIR", tmp_path / "brain")
    _db.reset()  # 2026-08 起为按租户连接池(_DBS),重置=全关全清
    yield tmp_path
    _db.reset()


@pytest.fixture
def server_mode(tmp_path, monkeypatch):
    """把 config 切到 server 模式(租户根指到临时目录),用完还原。

    共享夹具:test_tenancy / test_tenant_auth 等凡涉多租户的用例都用它。
    """
    from vococo import config

    monkeypatch.setattr(config, "IS_SERVER", True)
    monkeypatch.setattr(config, "TENANTS_DIR", tmp_path / "tenants", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "tenants"
