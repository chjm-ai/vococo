"""自我重启事务：遗书、回滚锚点与运行稳定状态必须彼此独立。"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    monkeypatch.setattr(
        selfops, "RESTART_FAILURE_PATH", tmp_path / "restart_failure.json"
    )
    monkeypatch.setattr(
        selfops, "RESTART_TRANSACTION_LOCK_PATH", tmp_path / ".restart_transaction.lock"
    )
    monkeypatch.setattr(selfops, "RESTART_STAMPS_PATH", tmp_path / "stamps.json")
    monkeypatch.setattr(selfops, "preflight", lambda: None)
    monkeypatch.setattr(selfops, "git_dirty", lambda: False)
    monkeypatch.setattr(selfops, "git_head", lambda: "candidate-sha")
    selfops._restart_pending.clear()
    yield tmp_path
    selfops._restart_pending.clear()


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def live_supervisor():
    """用独立进程模拟前台监督者，命令行带正式脚本绝对路径与角色参数。"""
    run_script = str((selfops._REPO_ROOT / "deploy" / "run.sh").resolve())
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            run_script,
            "--foreground",
        ]
    )
    selfops.SUPERVISOR_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    try:
        yield process
    finally:
        process.terminate()
        process.wait(timeout=5)


def _request(session_key="web:one", chat_id="chat-1"):
    return selfops.request_restart(
        platform="web",
        chat_id=chat_id,
        session_key=session_key,
        reason="加载新代码",
        verify_plan="检查健康状态",
    )


def test_restart_transaction_uses_previous_stable_and_current_candidate(
    live_supervisor,
):
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


def test_global_restart_transaction_is_single_flight(live_supervisor):
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


def test_corrupt_transaction_is_recovered_without_permanent_lock(live_supervisor):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    selfops.RESTART_TRANSACTION_PATH.write_text("{", encoding="utf-8")

    result = _request()

    assert result.startswith("✅")
    transaction = json.loads(
        selfops.RESTART_TRANSACTION_PATH.read_text(encoding="utf-8")
    )
    assert transaction["candidate_revision"] == "candidate-sha"
    assert not list(selfops.RESTART_TRANSACTION_PATH.parent.glob(".*.claim"))


def test_claim_cleanup_error_does_not_turn_published_transaction_into_failure(
    monkeypatch, capsys, live_supervisor
):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    real_unlink = Path.unlink

    def fail_claim_cleanup(path, *args, **kwargs):
        if path.name.endswith(".claim"):
            raise PermissionError("claim is read only")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_claim_cleanup)

    result = _request()

    assert result.startswith("✅")
    transaction = json.loads(
        selfops.RESTART_TRANSACTION_PATH.read_text(encoding="utf-8")
    )
    assert transaction["session_key"] == "web:one"
    assert "claim" in capsys.readouterr().out


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
    assert not list(selfops.RESUME_PATH.parent.glob(".*.tmp"))


def test_restart_refuses_unrelated_live_process_as_supervisor():
    process = subprocess.Popen(["/bin/sleep", "60"])
    selfops.SUPERVISOR_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    try:
        result = _request()
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert result.startswith("⛔")
    assert "监督者" in result
    assert not selfops.RESTART_TRANSACTION_PATH.exists()


def test_restart_refuses_without_a_stable_revision(live_supervisor):

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


def test_failed_state_write_leaves_no_orphan_resume(monkeypatch, live_supervisor):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})

    def fail_record(_recent, _token=None):
        raise OSError("disk full")

    monkeypatch.setattr(selfops, "_record_restart", fail_record)

    result = _request()

    assert result.startswith("⛔")
    assert not selfops.RESUME_PATH.exists()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()


def test_transaction_create_error_is_reported_without_crashing(
    monkeypatch, live_supervisor
):
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


def test_unserializable_resume_cleans_the_global_transaction(live_supervisor):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})

    result = _request(chat_id=object())

    assert result.startswith("⛔")
    assert "写入重启状态失败" in result
    assert not selfops.RESUME_PATH.exists()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()
    assert not selfops.restart_pending("web:one")
    assert not list(selfops.RESUME_PATH.parent.glob(".*.tmp"))


def test_exit_rechecks_supervisor_and_aborts_if_it_died(
    monkeypatch, live_supervisor
):
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    assert _request().startswith("✅")
    selfops.pop_restart_pending("web:one")  # 与 GatewayRunner 的真实调用顺序一致

    class Adapter:
        async def send(self, _chat_id, _text):
            live_supervisor.terminate()
            live_supervisor.wait(timeout=5)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(selfops.anyio, "sleep", no_sleep)
    monkeypatch.setattr(
        selfops.os, "_exit", lambda _code: pytest.fail("监督者已死时不得退出")
    )

    result = anyio.run(
        selfops.exit_for_restart, Adapter(), "chat-1", "web:one"
    )

    assert result is False
    assert not selfops.RESUME_PATH.exists()
    assert not selfops.RESTART_TRANSACTION_PATH.exists()
    failure = json.loads(selfops.RESTART_FAILURE_PATH.read_text(encoding="utf-8"))
    assert failure["session_key"] == "web:one"
    assert "监督者" in failure["reason"]


def test_cancelled_exit_removes_only_its_own_restart_stamp(
    monkeypatch, live_supervisor
):
    historical = {"token": "older-token", "requested_at": time.time() - 30}
    _write_json(selfops.RESTART_STAMPS_PATH, [historical])
    _write_json(selfops.STABLE_REVISION_PATH, {"revision": "stable-sha"})
    assert _request().startswith("✅")
    transaction = json.loads(
        selfops.RESTART_TRANSACTION_PATH.read_text(encoding="utf-8")
    )
    assert transaction["restart_token"] != historical["token"]
    live_supervisor.terminate()
    live_supervisor.wait(timeout=5)

    monkeypatch.setattr(
        selfops.os, "_exit", lambda _code: pytest.fail("监督者已死时不得退出")
    )
    assert selfops.supervisor_ready_for_exit("web:one") is False

    stamps = json.loads(selfops.RESTART_STAMPS_PATH.read_text(encoding="utf-8"))
    assert stamps == [historical]


def test_gateway_and_voice_exit_paths_use_the_shared_safe_exit():
    from vococo.gateway.run import GatewayRunner
    from vococo.voice import routes

    gateway_source = inspect.getsource(GatewayRunner._dispatch)
    voice_source = inspect.getsource(routes._handle_send)
    assert "exit_for_restart(adapter, inc.chat_id, inc.session_key)" in gateway_source
    assert "await selfops.exit_for_restart(" in voice_source
    assert "os._exit" not in voice_source


def test_git_dirty_includes_untracked_files(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="?? new-file\n", stderr="")

    monkeypatch.setattr(selfops, "_git", fake_git)
    monkeypatch.setattr(selfops, "git_dirty", _REAL_GIT_DIRTY)

    assert selfops.git_dirty() is True
    assert calls == [("status", "--porcelain")]


def test_git_dirty_fails_closed_when_status_returns_nonzero(monkeypatch):
    monkeypatch.setattr(
        selfops,
        "_git",
        lambda *_args: subprocess.CompletedProcess(
            ["git", "status"], 128, stdout="", stderr="not a repository"
        ),
    )
    monkeypatch.setattr(selfops, "git_dirty", _REAL_GIT_DIRTY)

    assert selfops.git_dirty() is True


@pytest.mark.parametrize(
    "error",
    [OSError("git missing"), subprocess.TimeoutExpired(["git", "status"], 30)],
)
def test_git_dirty_fails_closed_when_status_cannot_run(monkeypatch, error):
    def fail_git(*_args):
        raise error

    monkeypatch.setattr(selfops, "_git", fail_git)
    monkeypatch.setattr(selfops, "git_dirty", _REAL_GIT_DIRTY)

    assert selfops.git_dirty() is True


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


def test_stable_window_runs_git_head_outside_the_event_loop(monkeypatch):
    selfops.write_running_revision("candidate-sha")
    event_loop_thread = threading.get_ident()
    git_threads = []

    def observed_git_head():
        git_threads.append(threading.get_ident())
        return "candidate-sha"

    monkeypatch.setattr(selfops, "git_head", observed_git_head)

    assert anyio.run(selfops.mark_runtime_stable, 0) is True
    assert git_threads
    assert git_threads[0] != event_loop_thread
