"""冻结入口不触碰真实业务；仅测试拒绝路径与帮助信息。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
CASES = [
    ("anchor_ceramic_scan", []),
    ("anchor_ceramic_scan", ["--scan"]),
    ("anchor_gmail_scan", []),
    ("anchor_gmail_scan", ["--apply"]),
    ("anchor_monitor", []),
    ("anchor_monitor", ["--read-only", "--day", "2026-10-26"]),
    ("anchor_daily", []),
    ("anchor_daily", ["--raw-dir", "unused"]),
    ("anchor_checks", []),
    ("anchor_checks", ["--raw-dir", "unused"]),
]


@pytest.mark.parametrize("name,args", CASES)
def test_old_main_refuses_before_any_business_io(name, args, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        pytest.fail("冻结入口不得读取凭据、状态，访问网络或启动业务")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", [name, *args])
        for attr in ("Gmail", "scan", "read_only", "run_mailbox", "load_source",
                     "collect", "record", "latest_report", "collect_results", "replay_pending"):
            if hasattr(module, attr):
                patch.setattr(module, attr, forbidden)
        patch.setattr(Path, "read_text", forbidden)
        patch.setattr(Path, "write_text", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(subprocess, "run", forbidden)
        result = module.main()
    assert result == 1
    text = capsys.readouterr().out
    assert "已停用" in text and "anchor-mail-ops" in text
    assert text.count("##CRON_SIGNAL:1##") == 1


@pytest.mark.parametrize("name,args", CASES)
def test_real_old_cli_exits_nonzero_without_creating_files(name, args, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py"), *args],
        cwd=tmp_path, env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1
    assert "anchor-mail-ops" in result.stdout
    assert "##CRON_SIGNAL:1##" in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", sorted({name for name, _ in CASES}))
def test_help_marks_entry_frozen(name, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py"), "--help"],
        cwd=tmp_path, env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0
    assert "已冻结" in result.stdout and "anchor-mail-ops" in result.stdout
    assert not list(tmp_path.iterdir())
