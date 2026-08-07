"""自我重启事务：遗书、回滚锚点与运行稳定状态必须彼此独立。"""
from __future__ import annotations

import json
import os
import subprocess

import anyio
import pytest

from vococo.tools import selfops

_REAL_GIT_DIRTY = selfops.git_dirty


@pytest.fixture(autouse=True)
def isolated_selfops(tmp_path, monkeypatch):
    """所有运行态文件放到临时目录，且不执行真实预检/git。"""
    monkeypatch.setattr(selfops, "RESUME_PATH", tmp_path / "resume_task.json")
    monkeypatch.setattr(
        selfops, "RESTART_TRANSACTION_PATH", tmp_path / "restart_transaction.json"
    )
    monkeypatch.setattr(
        selfops, "RUNNING_REVISION_PATH", tmp_path / "running_revision.json"
    )
    monkeypatch.setattr(
        selfops, "STABLE_REVISION_PATH", tmp_path / "stable_revision.json"
    )
    monkeypatch.setattr(selfops, "SUPERVISOR_PID_PATH", tmp_path / "supervisor.pid")
    monkeypatch.setattr(selfops, "RESTART_STAMPS_PATH", tmp_path / "stamps.json")
    monkeypatch.setattr(selfops, "preflight", lambda: None)
    monkeypatch.setattr(selfops, "git_dirty", lambda: False)
    monkeypatch.setattr(selfops, "git_head", lambda: "candidate-sha")
    selfops._restart_pending.clear()
    yield tmp_path
    selfops._restart_pending.clear()


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_supervisor_alive():
    selfops.SUPERVISOR_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _request(session_key="web:one"):
    return selfops.request_restart(
        platform="web",
        chat_id="chat-1",
        session_key=session_key,
        reason="加载新代码",
        verify_plan="检查健康状态",
    )


def test_restart_transaction_uses_previous_stable_and_current_candidate():
    _make_supervisor_alive()
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})

    result = _request()

    assert result.startswith("✅")
    transaction = json.loads(
        selfops.RESTART_TRANSACTION_PATH.read_text(encoding="utf-8")
    )
    assert transaction["stable_revision"] == "stable-sha"
    assert transaction["candidate_revision"] == "candidate-sha"
    assert transaction["session_key"] == "web:one"
    resume = json.loads(selfops.RESUME_PATH.read_text(encoding="utf-8"))
    assert resume["rollback_commit"] == "stable-sha"
    assert resume["candidate_revision"] == "candidate-sha"


def test_global_restart_transaction_is_single_flight():
    _make_supervisor_alive()
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    assert _request("web:first").startswith("✅")
    selfops._restart_pending.clear()  # 模拟另一个进程/会话，不依赖内存锁

    result = _request("web:second")

    assert result.startswith("⛔")
    assert "全局重启事务" in result
    transaction = json.loads(
        selfops.RESTART_TRANSACTION_PATH.read_text(encoding="utf-8")
    )
    assert transaction["session_key"] == "web:first"


@pytest.mark.parametrize("pid_text", [None, "not-a-pid", "99999999"])
def test_restart_refuses_when_supervisor_is_not_alive(pid_text):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    if pid_text is not None:
        selfops.SUPERVISOR_PID_PATH.write_text(pid_text, encoding="utf-8")

    result = _request()

    assert result.startswith("⛔")
    assert "监督者" in result
    assert not selfops.RESUME_PATH.exists()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()
    assert not selfops.restart_pending("web:one")


def test_restart_refuses_without_a_stable_revision():
    _make_supervisor_alive()

    result = _request()

    assert result.startswith("⛔")
    assert "稳定版本" in result
    assert not selfops.RESTART_TRANSACTION_PATH.exists()


def test_consuming_resume_keeps_restart_transaction_for_rollback():
    _write_json(selfops.RESUME_PATH, {"session_key": "web:one"})
    _write_json(
        selfops.RESTART_TRANSACTION_PATH,
        {"stable_revision": "stable-sha", "candidate_revision": "candidate-sha"},
    )

    assert selfops.consume_resume() == {"session_key": "web:one"}

    assert not selfops.RESUME_PATH.exists()
    assert selfops.RESTART_TRANSACTION_PATH.exists()


def test_failed_state_write_leaves_no_orphan_resume(monkeypatch):
    _make_supervisor_alive()
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})

    def fail_record(_recent):
        raise OSError("disk full")

    monkeypatch.setattr(selfops, "_record_restart", fail_record)

    result = _request()

    assert result.startswith("⛔")
    assert not selfops.RESUME_PATH.exists()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()


def test_transaction_create_error_is_reported_without_crashing(monkeypatch):
    _make_supervisor_alive()
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    monkeypatch.setattr(
        selfops,
        "_create_restart_transaction",
        lambda _data: (_ for _ in ()).throw(OSError("read only")),
    )

    result = _request()

    assert result.startswith("⛔")
    assert "重启事务" in result
    assert not selfops.RESUME_PATH.exists()


def test_git_dirty_includes_untracked_files(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="?? new-file\n", stderr="")

    monkeypatch.setattr(selfops, "_git", fake_git)
    monkeypatch.setattr(selfops, "git_dirty", _REAL_GIT_DIRTY)

    assert selfops.git_dirty() is True
    assert calls == [("status", "--porcelain")]


def test_stable_window_promotes_running_revision_and_clears_transaction():
    _write_json(
        selfops.RESTART_TRANSACTION_PATH,
        {"stable_revision": "stable-sha", "candidate_revision": "candidate-sha"},
    )
    selfops.write_running_revision("candidate-sha")

    anyio.run(selfops.mark_runtime_stable, 0)

    stable = json.loads(selfops.STABLE_REVISION_PATH.read_text(encoding="utf-8"))
    assert stable["revision"] == "candidate-sha"
    assert stable["pid"] == os.getpid()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()


def test_stable_window_does_not_clear_a_different_candidate():
    _write_json(
        selfops.RESTART_TRANSACTION_PATH,
        {"stable_revision": "stable-sha", "candidate_revision": "other-sha"},
    )
    selfops.write_running_revision("candidate-sha")

    anyio.run(selfops.mark_runtime_stable, 0)

    assert selfops.RESTART_TRANSACTION_PATH.exists()
