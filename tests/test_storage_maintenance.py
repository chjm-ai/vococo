"""缓存回收只动明确可重建文件；原件、配置、依赖和未知内容始终保留。"""
import json
import os
from pathlib import Path

import pytest

from vococo.core import storage_maintenance as storage

_DAY = 86400
_NOW = 1_800_000_000.0


def old_file(path, days=31):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test" * 1024)
    os.utime(path, (_NOW - days * _DAY, _NOW - days * _DAY))
    return path


def test_active_business_copies_are_counted_without_cleanup_candidates(tmp_path):
    data, copies = tmp_path / "data", tmp_path / ".repositories"
    data.mkdir()
    file = old_file(copies / "owner/session/repo/.venv/large")
    report = storage.maintain(data.resolve(), repositories=copies, now=_NOW)
    assert report["sizes"]["工作树"] == file.stat().st_blocks * 512
    assert file.exists()


def test_all_known_runtime_databases_and_wals_are_counted(tmp_path):
    files = [old_file(tmp_path / (name + suffix))
             for name in ("state.db", "voice/voice.db", "stats.db", "library.db", "mcp_events.db", "research.db")
             for suffix in ("", "-wal", "-shm")]
    report = storage.maintain(tmp_path.resolve(), now=_NOW)
    assert report["sizes"]["数据库"] == sum(file.stat().st_blocks * 512 for file in files)
    assert all(file.exists() for file in files)


def test_empty_or_nonmanaged_receipt_paths_are_not_scanned():
    from vococo.core import repository_cleanup
    rows = [{"path": ""}, {"path": "."}, {"path": "/"}, {"path": "/unrelated"},
            {"path": "/project/data/worktrees/owner/task"}]
    assert repository_cleanup._storage_worktrees(rows) == [Path("/project/data/worktrees/owner/task")]


def test_only_expired_known_caches_are_removed(tmp_path):
    data = tmp_path.resolve()
    source = old_file(data / "videos" / "123_0.mov")
    preview = old_file(data / "videos" / "preview" / "123_0.mov.mp4")
    prompt = old_file(data / "prompt_cache" / ("a" * 64 + ".json"))
    kept = [old_file(data / "prompt_cache" / ("b" * 64 + ".json"), 29),
            old_file(data / "prompt_cache" / "notes.json"),
            old_file(data / "videos" / "preview" / "unknown.mp4"),
            old_file(data / "videos" / "preview" / "999_0.mov.mp4"),
            old_file(data / "state.db"), old_file(data / "private/session-recovery/backup.db"),
            old_file(data / "worktrees/test/.venv/config"), old_file(data / "files/user.txt")]
    report = storage.maintain(data, now=_NOW)
    assert not prompt.exists() and not preview.exists()
    assert source.exists() and all(path.exists() for path in kept)
    assert report["caches"]["prompt"]["removed"] == 1
    assert report["caches"]["preview"]["removed"] == 1
    assert not report["errors"]
    assert json.loads((data / "storage-maintenance.json").read_text()) == report


@pytest.mark.parametrize("days,expired", [(29, False), (30, True), (-1, False)])
def test_cache_age_boundary(tmp_path, days, expired):
    file = old_file(tmp_path / "prompt_cache" / ("a" * 64 + ".json"), days)
    storage.maintain(tmp_path.resolve(), now=_NOW)
    assert file.exists() != expired


@pytest.mark.parametrize("link_kind", ["file", "directory", "parent"])
def test_symlinks_never_allow_cache_cleanup_outside_data(tmp_path, link_kind):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    target = old_file(outside / ("a" * 64 + ".json"))
    if link_kind == "file":
        (data / "prompt_cache").mkdir()
        (data / "prompt_cache" / target.name).symlink_to(target)
    elif link_kind == "directory":
        (data / "prompt_cache").symlink_to(outside, target_is_directory=True)
    else:
        (data / "videos").symlink_to(outside, target_is_directory=True)
        old_file(outside / "preview" / "123_0.mov.mp4")
        old_file(outside / "123_0.mov")
    storage.maintain(data.resolve(), now=_NOW)
    assert target.exists()
    if link_kind == "parent":
        assert (outside / "preview" / "123_0.mov.mp4").exists()


def test_daily_samples_are_bounded_and_ignore_frequent_runs(tmp_path):
    storage.maintain(tmp_path.resolve(), now=_NOW)
    report = storage.maintain(tmp_path.resolve(), now=_NOW + 60)
    assert len(report["samples"]) == 1
    for day in range(1, 40):
        report = storage.maintain(tmp_path.resolve(), now=_NOW + day * _DAY)
    assert len(report["samples"]) == 31
    assert report["samples"][0]["at"] == _NOW + 9 * _DAY


def test_capacity_scan_does_not_double_count_nested_paths_or_hardlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    checkout = data / "worktrees" / "owner" / "task"
    file = old_file(checkout / "file")
    os.link(file, checkout / "hardlink")
    report = storage.maintain(data.resolve(), worktrees=[checkout], now=_NOW)
    assert report["sizes"]["工作树"] == file.stat().st_blocks * 512


def test_report_symlink_is_rejected_without_overwriting_or_cleanup(tmp_path):
    outside = old_file(tmp_path / "outside.json")
    data = tmp_path / "data"
    data.mkdir()
    cache = old_file(data / "prompt_cache" / ("a" * 64 + ".json"))
    (data / "storage-maintenance.json").symlink_to(outside)
    with pytest.raises(ValueError):
        storage.maintain(data.resolve(), now=_NOW)
    assert cache.exists() and outside.read_bytes() == b"test" * 1024


def test_failed_cache_cleanup_does_not_prevent_other_inventory(tmp_path, monkeypatch):
    data = tmp_path.resolve()
    old_file(data / "state.db")
    def failed(*args):
        raise PermissionError("SECRET")
    monkeypatch.setattr(storage, "_expire_cache", failed)
    report = storage.maintain(data, now=_NOW)
    assert report["sizes"]["数据库"] > 0 and len(report["errors"]) == 2
    assert "SECRET" not in json.dumps(report)
