from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("等待进程状态超时")


def _pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "vococo-runtime"
    deploy = root / "deploy"
    bin_dir = root / ".venv" / "bin"
    deploy.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    (root / "data" / "logs").mkdir(parents=True)
    for name in ("run.sh", "restart.sh", "stop.sh"):
        shutil.copy2(REPO_ROOT / "deploy" / name, deploy / name)

    fake_serve = bin_dir / "vococo"
    fake_serve.write_text(
        "#!/bin/sh\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    fake_serve.chmod(0o755)

    env = os.environ.copy()
    env["VOCOCO_RESTART_DELAY"] = "0.05"
    env["VOCOCO_RESTART_ATTEMPTS"] = "10"
    return root, env


def _start_supervisor(root: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        ["zsh", str(root / "deploy" / "run.sh"), "--foreground"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(lambda: (root / "data" / "child.pid").exists())
    except AssertionError:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"监督者提前退出({process.returncode}): {output}")
        raise
    return process


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def test_foreground_rejects_duplicate_supervisor(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    try:
        duplicate = subprocess.run(
            ["zsh", str(root / "deploy" / "run.sh"), "--foreground"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=3,
        )
        assert duplicate.returncode != 0
        assert "监督者已在运行" in duplicate.stdout + duplicate.stderr
        assert _pid(root / "data" / "supervisor.pid") == supervisor.pid
        assert supervisor.poll() is None
    finally:
        subprocess.run(
            ["zsh", str(root / "deploy" / "stop.sh")],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=3,
        )
        _stop_process(supervisor)


def test_restart_replaces_only_recorded_child(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    unrelated = subprocess.Popen(
        [str(root / ".venv" / "bin" / "vococo"), "serve"], env=env
    )
    try:
        old_child = _pid(root / "data" / "child.pid")
        result = subprocess.run(
            ["zsh", str(root / "deploy" / "restart.sh"), "--force"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        new_child = _pid(root / "data" / "child.pid")
        assert new_child != old_child
        assert _is_alive(new_child)
        assert unrelated.poll() is None
    finally:
        subprocess.run(
            ["zsh", str(root / "deploy" / "stop.sh")],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=3,
        )
        _stop_process(supervisor)
        _stop_process(unrelated)


def test_restart_cleans_stale_child_pid(runtime) -> None:
    root, env = runtime
    child_pid = root / "data" / "child.pid"
    child_pid.write_text("99999999\n", encoding="utf-8")

    result = subprocess.run(
        ["zsh", str(root / "deploy" / "restart.sh"), "--force"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode != 0
    assert "PID 已失效" in result.stdout + result.stderr
    assert not child_pid.exists()


def test_restart_never_kills_unrelated_reused_pid(runtime) -> None:
    root, env = runtime
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        child_pid = root / "data" / "child.pid"
        child_pid.write_text(f"{unrelated.pid}\n", encoding="utf-8")
        result = subprocess.run(
            ["zsh", str(root / "deploy" / "restart.sh"), "--force"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=3,
        )

        assert result.returncode != 0
        assert "不属于本仓库" in result.stdout + result.stderr
        assert unrelated.poll() is None
        assert not child_pid.exists()
    finally:
        _stop_process(unrelated)


def test_restart_rejects_same_command_from_another_parent(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    real_child = _pid(root / "data" / "child.pid")
    unrelated = subprocess.Popen(
        [str(root / ".venv" / "bin" / "vococo"), "serve"], env=env
    )
    try:
        (root / "data" / "child.pid").write_text(
            f"{unrelated.pid}\n", encoding="utf-8"
        )
        result = subprocess.run(
            ["zsh", str(root / "deploy" / "restart.sh"), "--force"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=2,
        )

        assert result.returncode != 0
        assert "不属于当前监督者" in result.stdout + result.stderr
        assert unrelated.poll() is None
    finally:
        (root / "data" / "child.pid").write_text(
            f"{real_child}\n", encoding="utf-8"
        )
        subprocess.run(
            ["zsh", str(root / "deploy" / "stop.sh")],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=3,
        )
        _stop_process(supervisor)
        _stop_process(unrelated)


def test_term_exits_supervisor_and_its_child(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    child = _pid(root / "data" / "child.pid")
    try:
        supervisor.terminate()
        supervisor.wait(timeout=2)
        _wait_for(lambda: not _is_alive(child), timeout=2)
        assert not (root / "data" / "supervisor.pid").exists()
        assert not (root / "data" / "child.pid").exists()
    finally:
        (root / "data" / ".stop").touch()
        _stop_process(supervisor)


def test_stop_prevents_supervisor_from_relaunching_child(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    old_child = _pid(root / "data" / "child.pid")

    result = subprocess.run(
        ["zsh", str(root / "deploy" / "stop.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    supervisor.wait(timeout=3)
    assert (root / "data" / ".stop").exists()
    assert not _is_alive(old_child)
    time.sleep(0.2)
    assert not (root / "data" / "child.pid").exists()
    assert not (root / "data" / "supervisor.pid").exists()


def test_stop_never_kills_same_command_unowned_child(runtime) -> None:
    root, env = runtime
    supervisor = _start_supervisor(root, env)
    unrelated = subprocess.Popen(
        [str(root / ".venv" / "bin" / "vococo"), "serve"], env=env
    )
    try:
        (root / "data" / "child.pid").write_text(
            f"{unrelated.pid}\n", encoding="utf-8"
        )
        result = subprocess.run(
            ["zsh", str(root / "deploy" / "stop.sh")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=3,
        )

        assert result.returncode == 0
        assert "不属于当前监督者" in result.stdout + result.stderr
        assert unrelated.poll() is None
        supervisor.wait(timeout=2)
    finally:
        _stop_process(supervisor)
        _stop_process(unrelated)
