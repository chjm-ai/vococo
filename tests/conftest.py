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
    monkeypatch.setattr(_db, "_DB", None)
    yield tmp_path
    _db.reset()


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch, request):
    """审批记录/永久规则默认不落真实 state.db(tools/danger.py 每次审批都会写)。
    需要真实读写的用例请用 isolated 夹具——它会把库指到临时目录,这里就不再拦。"""
    if "isolated" in request.fixturenames:
        return
    from vococo.memory import approvals

    monkeypatch.setattr(approvals, "log", lambda **kw: None)
    monkeypatch.setattr(approvals, "match_rule", lambda kind, target: None)
    monkeypatch.setattr(approvals, "add_rule", lambda *a, **kw: {})
    monkeypatch.setattr(approvals, "add_missed", lambda **kw: "")
