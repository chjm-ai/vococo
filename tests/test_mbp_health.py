"""MBP 巡检解析、缺失数据与重启边界。"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "mbp_health", Path(__file__).resolve().parents[1] / "scripts/mbp_health.py"
)
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)
NOW = datetime(2026, 10, 27, 20, 17, tzinfo=timezone.utc)


def row(minutes=0, boot="a", swap=10):
    return {"timestamp": (NOW - timedelta(minutes=minutes)).isoformat(), "boot": boot,
            "vm": {"swapout_mb": swap}, "cpu_pct": 85, "pressure": 1, "errors": {}}


def test_parsers_use_last_cpu_and_actual_page_size():
    assert health.parse_top("CPU usage: 0% user, 0% sys, 90.0% idle\n"
                            "CPU usage: 2% user, 8% sys, 80.5% idle") == 19.5
    assert health.parse_vm('page size of 16384 bytes\nSwapins: 64.\nSwapouts: 128.') == {
        "swapin_mb": 1, "swapout_mb": 2}
    assert health.parse_swap("used = 1.50G") == 1536
    assert health.parse_thermal("CPU_Speed_Limit = 80")["limited"] is True
    assert health.parse_thermal("No performance warning\nNo thermal warning")["limited"] is False


@pytest.mark.parametrize("parser", [health.parse_top, health.parse_vm,
                                    health.parse_swap, health.parse_thermal])
def test_parser_failures_do_not_become_healthy_values(parser):
    with pytest.raises(ValueError):
        parser("unexpected output")


def test_swap_delta_excludes_reboot_gap_and_reset():
    assert health.swap_delta([row(5, swap=10), row(swap=30)]) == (20, 1)
    assert health.swap_delta([row(5), row(boot="b", swap=30)]) == (0, 0)
    assert health.swap_delta([row(20), row(swap=30)]) == (0, 0)
    assert health.swap_delta([row(5, swap=30), row(swap=10)]) == (0, 0)


def test_read_window_handles_missing_and_corrupt_logs(tmp_path):
    assert health.read_window(tmp_path, NOW) == ([], 0)
    folder = tmp_path / "samples"
    folder.mkdir()
    import json
    (folder / "2026-10-27.jsonl").write_text(
        json.dumps(row(5)) + '\ninvalid\n' + json.dumps(row(1500)) + '\n')
    rows, damaged = health.read_window(tmp_path, NOW)
    assert len(rows) == 1
    assert damaged == 1


def test_read_window_skips_truncated_utf8(tmp_path):
    folder = tmp_path / "samples"
    folder.mkdir()
    (folder / "2026-10-27.jsonl").write_bytes(b'\xe4\xb8\n')
    assert health.read_window(tmp_path, NOW) == ([], 1)


def test_thermal_detects_reduced_cpu_count(monkeypatch):
    monkeypatch.setattr(health.os, "cpu_count", lambda: 8)
    result = health.parse_thermal("CPU_Speed_Limit = 100\nCPU_Scheduler_Limit = 100\nCPU_Available_CPUs = 4")
    assert result["limited"] is True


def test_report_is_explicit_about_missing_data():
    assert "无法判断" in health.render_report([], 0, NOW)
    text = health.render_report([row()], 1, NOW)
    assert "未采集成功" in text
    assert "磁盘最低剩余：未知" in text
    assert "损坏日志：1 行" in text
    assert "不能换算" in text


def test_prune_only_removes_old_dated_files(tmp_path):
    folder = tmp_path / "samples"
    folder.mkdir()
    old = folder / "2026-01-01.jsonl"
    keep = folder / "2026-10-27.jsonl"
    other = folder / "diagnostic.log"
    for path in (old, keep, other):
        path.touch()
    health.prune(tmp_path, NOW)
    assert not old.exists()
    assert keep.exists() and other.exists()


def test_probe_errors_are_persisted(monkeypatch):
    def fail(*args):
        raise OSError("probe unavailable")
    monkeypatch.setattr(health, "run", fail)
    result = health.collect()
    assert "cpu_pct" not in result
    assert "cpu_pct" in result["errors"]
    assert result["disk_free_gb"] > 0
