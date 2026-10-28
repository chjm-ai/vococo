"""专用养号标记与存量人工队列迁移的回归测试。"""
import json
from pathlib import Path

import pytest

from test_anchor_reply_association import Client, message
import anchor_gmail_scan as gmail
import anchor_ceramic_scan as ceramic


@pytest.mark.parametrize("subject", [
    "Re: moodboard reaction - lemwarmup", "Workshop - LEMWARMUP   ",
    "Re: storyboard - lemwarmup 0A3GN9JM",
    "=?UTF-8?Q?Re=3A_workshop_-_lemwarmup?=",
])
def test_product_marker_without_random_suffix_is_excluded(subject):
    client = Client()
    assert gmail.classify(client, message(subject=subject)) == "excluded"
    assert not client.reads


@pytest.mark.parametrize("subject", [
    "Re: warm insulation", "Re: warmup schedule", "Re: storyboard feedback",
    "Re: lemwarmup integration question", "Re: notlemwarmup",
    "Re: workshop - lemwarmup customer question", "Re: workshop - lemwarmup abc",
])
def test_ordinary_customer_words_are_not_product_markers(subject):
    client = Client()
    assert gmail.classify(client, message(subject=subject)) == "customer"


@pytest.mark.parametrize("scanner,key", [(gmail.scan, "messages"), (ceramic.scan, "delivery")])
def test_manual_migration_backs_up_and_is_idempotent_without_sending(tmp_path, scanner, key):
    client, path, now = Client(), tmp_path / "state.json", gmail.NOT_BEFORE + 1000
    state = {"address": client.address, "enabled_at": now, "last_ts": now,
             "messages": {}, "delivery": {}, "pending": [], "forwarded_ids": ["already"]}
    state[key] = {"incident": "manual", "customer": "manual", "unclear": "uncertain", "old": "old"}
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    client.ids = lambda query: []
    def get(url, **params):
        return message(subject="Re: topic - lemwarmup" if url.endswith("incident") else "Re: warmup schedule")
    client.get = get
    result = scanner(client, path, now)
    assert result["reclassified_manual_ids"] == ["incident"]
    backup = result["manual_migration_backup"]
    assert Path(backup).read_bytes() == before
    assert Path(backup).stat().st_mode & 0o777 == 0o600
    migrated = json.loads(path.read_text())
    assert migrated[key]["incident"] == "excluded"
    assert migrated[key]["customer"] == "manual"
    assert migrated[key]["unclear"] == "uncertain"
    assert migrated[key]["old"] == "old"
    assert migrated["forwarded_ids"] == ["already"]
    assert result["coverage"] == "partial"
    assert not client.sent
    second = scanner(client, path, now + 1)
    assert second["reclassified_manual_ids"] == []
    assert second["manual_migration_backup"] is None
    assert len(list(tmp_path.glob("*.bak"))) == 1


@pytest.mark.parametrize("scanner,key", [(gmail.scan, "messages"), (ceramic.scan, "delivery")])
def test_manual_read_failure_stays_visible_and_does_not_migrate(tmp_path, scanner, key):
    client, path, now = Client(), tmp_path / "state.json", gmail.NOT_BEFORE + 1000
    state = {"address": client.address, "enabled_at": now, "last_ts": now,
             "messages": {}, "delivery": {}, "pending": [], "forwarded_ids": []}
    state[key] = {"incident": "manual"}
    path.write_text(json.dumps(state))
    client.ids = lambda query: []
    client.fail = True
    result = scanner(client, path, now)
    assert result["coverage"] == "partial"
    assert result["manual_review_ids"] == ["incident"]
    assert "incident" in result["errors"][0]
    assert result["reclassified_manual_ids"] == []
    assert not list(tmp_path.glob("*.bak"))
    assert not client.sent


def test_removed_marker_on_reply_is_excluded_by_unsuffixed_original():
    client = Client()
    client.original["payload"]["headers"][1]["value"] = "Workshop - lemwarmup"
    assert gmail.classify(client, client.incoming) == "excluded"


def test_backup_failure_does_not_change_manual_state(tmp_path, monkeypatch):
    client, path = Client(), tmp_path / "state.json"
    state = {"messages": {"incident": "manual"}}
    path.write_text(json.dumps(state))
    client.incoming = message(subject="Workshop - lemwarmup")
    before = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("模拟备份失败")
    monkeypatch.setattr(gmail, "backup_state", fail)
    with pytest.raises(OSError, match="备份失败"):
        gmail.reclassify_manual(client, state, path, "messages")
    assert state["messages"]["incident"] == "manual"
    assert path.read_bytes() == before
