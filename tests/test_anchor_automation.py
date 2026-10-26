"""全部外发用内存替身；禁止测试访问真实邮箱或客户。"""
import ast
import base64
import importlib.util
import json
from datetime import date
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gmail = load_script("anchor_gmail_scan")
monitor = load_script("anchor_monitor")


def message(timestamp, **headers):
    defaults = {"From": "Buyer <buyer@example.test>", "Subject": "Re: material",
                "In-Reply-To": "<original@example.test>"}
    defaults.update(headers)
    return {"internalDate": str(timestamp * 1000), "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": key, "value": value} for key, value in defaults.items()]}}


class FakeGmail:
    address = "ray@anchor-filter.com"
    scopes = gmail.READ_SCOPES | gmail.SEND_SCOPES

    def __init__(self, timestamp):
        self.timestamp = timestamp
        self.messages = {"new": message(timestamp), "old": message(timestamp - 100)}
        self.calls, self.sent, self.fail = [], [], set()
        self.send_status = "sent"
        self.reconciled = False

    def ids(self, query):
        self.calls.append(query)
        if "rfc822msgid" in query:
            return ["receipt"] if self.reconciled else []
        return list(self.messages)

    def get(self, path, **params):
        mid = path.rsplit("/", 1)[-1]
        if mid in self.fail:
            raise gmail.ScanError("模拟读取失败")
        if params.get("format") == "raw":
            raw = b"From: buyer@example.test\r\nSubject: Re: material\r\n\r\nInterested."
            return {"raw": base64.urlsafe_b64encode(raw).decode()}
        return self.messages[mid]

    def send(self, raw):
        self.sent.append(message_from_bytes(base64.urlsafe_b64decode(raw)))
        return self.send_status


def test_first_enable_skips_history_and_simulated_send_is_deduplicated(tmp_path):
    now = gmail.NOT_BEFORE + 1000
    client, path = FakeGmail(now), tmp_path / "state.json"
    first = gmail.scan(client, path, now)
    second = gmail.scan(client, path, now + 60)
    assert first["coverage"] == second["coverage"] == "ok"
    assert len(client.sent) == 1
    assert client.sent[0]["To"] == gmail.DESTINATION
    assert client.sent[0]["X-Anchor-Forward"] == "1"
    assert client.sent[0].get_payload()[1].get_content_type() == "message/rfc822"
    assert json.loads(path.read_text())["messages"]["old"] == "old"


@pytest.mark.parametrize("headers", [
    {"From": "ray.chen@anchorgd.com"}, {"From": "ray@anchor-microporous.com"},
    {"From": "MAILER-DAEMON@example.test"}, {"From": "noreply@example.test"},
    {"From": "challenge@mailinblack.com"}, {"Auto-Submitted": "auto-replied"},
    {"Precedence": "bulk"}, {"List-Id": "newsletter"}, {"X-Anchor-Forward": "1"},
    {"Subject": "Out of office"}, {"In-Reply-To": ""}, {"From": "invalid"},
])
def test_system_self_loop_and_non_reply_are_excluded(headers):
    assert not gmail.is_reply(message(gmail.NOT_BEFORE, **headers))


def test_metadata_failure_is_retained_across_watermark(tmp_path):
    now = gmail.NOT_BEFORE + 1000
    client, path = FakeGmail(now), tmp_path / "state.json"
    client.fail.add("new")
    assert gmail.scan(client, path, now)["coverage"] == "partial"
    assert json.loads(path.read_text())["messages"]["new"] == "retry"
    client.fail.clear()
    client.ids = lambda query: []
    assert gmail.scan(client, path, now + 7200)["coverage"] == "ok"
    assert len(client.sent) == 1


def test_known_failure_retries_and_uncertain_result_never_blindly_resends(tmp_path):
    now = gmail.NOT_BEFORE + 1000
    client, path = FakeGmail(now), tmp_path / "state.json"
    client.send_status = "retry"
    assert gmail.scan(client, path, now)["coverage"] == "partial"
    client.send_status = "uncertain"
    assert gmail.scan(client, path, now + 1)["coverage"] == "partial"
    count = len(client.sent)
    assert gmail.scan(client, path, now + 2)["coverage"] == "partial"
    assert len(client.sent) == count
    client.reconciled = True
    assert gmail.scan(client, path, now + 3)["coverage"] == "ok"
    assert len(client.sent) == count


def test_list_failure_does_not_advance_state(tmp_path):
    now = gmail.NOT_BEFORE + 1000
    client, path = FakeGmail(now), tmp_path / "state.json"
    gmail.scan(client, path, now)
    previous = path.read_text()
    client.ids = lambda query: (_ for _ in ()).throw(gmail.ScanError("list failed"))
    with pytest.raises(gmail.ScanError):
        gmail.scan(client, path, now + 100)
    assert path.read_text() == previous


def test_mailbox_states_are_isolated_and_wrong_state_fails(tmp_path):
    now = gmail.NOT_BEFORE + 1000
    a, b = FakeGmail(now), FakeGmail(now)
    b.address = "ray@anchor-microporous.com"
    gmail.scan(a, tmp_path / "a.json", now)
    gmail.scan(b, tmp_path / "b.json", now)
    assert len(a.sent) == len(b.sent) == 1
    assert a.sent[0]["Message-ID"] != b.sent[0]["Message-ID"]
    with pytest.raises(gmail.ScanError):
        gmail.scan(b, tmp_path / "a.json", now)


def test_missing_send_scope_and_corrupt_state_fail_closed(tmp_path):
    client = FakeGmail(gmail.NOT_BEFORE + 1000)
    client.scopes = {"https://www.googleapis.com/auth/gmail.readonly"}
    with pytest.raises(gmail.ScanError, match="scope"):
        gmail.scan(client, tmp_path / "state.json", client.timestamp)
    assert not client.sent
    client.scopes = gmail.READ_SCOPES | gmail.SEND_SCOPES
    path = tmp_path / "state.json"
    path.write_text("invalid")
    with pytest.raises(ValueError):
        gmail.scan(client, path, client.timestamp)
    assert not client.sent


def test_gmail_pagination_and_page_error():
    client = object.__new__(gmail.Gmail)
    calls = []
    def get(path, **params):
        calls.append(params)
        return ({"messages": [{"id": "b"}]} if "pageToken" in params else
                {"messages": [{"id": "a"}], "nextPageToken": "next"})
    client.get = get
    assert client.ids("query") == ["a", "b"]
    assert calls[1]["pageToken"] == "next"
    assert all(call["includeSpamTrash"] is True for call in calls)
    client.get = lambda *a, **kw: (_ for _ in ()).throw(gmail.ScanError("failed"))
    with pytest.raises(gmail.ScanError):
        client.ids("query")


def fake_monitor_api():
    cfg = {"bounce_window_days": 7, "anchor_alert_threshold": .03, "bounce_min_sample": 50}
    def lem(method, path):
        assert method == "GET"
        if "/activities?" in path:
            return []
        if "/leads?" in path:
            return [{"state": "reviewed"}]
        return {"_id": path.rsplit("/", 1)[-1], "status": "running", "name": "ANCHOR-test"}
    return SimpleNamespace(lem=lem, export_leads=lambda *a: [{}], load_cfg=lambda: cfg)


def test_running_and_reviewed_are_not_sends_and_only_anchor_is_collected(tmp_path):
    result = monitor.collect(fake_monitor_api(), date(2026, 10, 26))
    assert set(result["campaigns"]) == set(monitor.CAMPAIGNS)
    for row in result["campaigns"].values():
        assert row["reviewed_state"] == row["sendable_export"] == 1
        assert row["emailsSent"]["events_all"] == row["emailsSent"]["leads_all"] == 0
    monitor.record(result, tmp_path)
    assert len((tmp_path / "隔热指标.jsonl").read_text().splitlines()) == 1


def test_partial_failure_is_not_reported_as_zero_and_export_failure_is_visible():
    api = fake_monitor_api()
    api.lem = lambda *a: None
    result = monitor.collect(api, date(2026, 10, 26))
    assert all(row["coverage"] == "failed" and "emailsSent" not in row for row in result["campaigns"].values())
    api = fake_monitor_api()
    api.export_leads = lambda *a: None
    row = monitor.campaign_snapshot(api, "cam_test", date(2026, 10, 26))
    assert row["sendable_export"] is None and row["warnings"]


def test_lemlist_pagination_has_no_20000_cap_and_failure_raises():
    calls = []
    def lem(method, path):
        calls.append(path)
        return [{"leadId": str(i)} for i in range(100)] if "offset=0" in path else []
    assert len(monitor.fetch_pages(SimpleNamespace(lem=lem), "/activities?type=emailsSent", 100)) == 100
    assert "offset=100" in calls[-1]
    with pytest.raises(ValueError):
        monitor.fetch_pages(SimpleNamespace(lem=lambda *a: {"error": "failure"}), "/leads", 500)


def test_event_count_distinct_leads_and_rolling_threshold():
    rows = [{"leadId": "same", "createdAt": "2026-10-26T06:00:00Z"}] * 3
    assert len(monitor.unique_leads(rows)) == 1
    with pytest.raises(ValueError):
        monitor.unique_leads([{}])
    rows = [{"leadId": str(i), "createdAt": "2026-10-26T06:00:00Z"} for i in range(50)]
    end = monitor.datetime(2026, 10, 27, tzinfo=monitor.CN)
    stats, alert = monitor.rolling_bounce({"emailsSent": rows, "emailsBounced": rows[:2]}, end,
                                         fake_monitor_api().load_cfg())
    assert stats["new_leads"] == 50 and alert


def test_missing_credentials_make_no_network_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail.requests, "post", lambda *a, **kw: pytest.fail("不应请求网络"))
    result = gmail.run_mailbox("ray@anchor-filter.com", tmp_path / "absent.json", False)
    assert result["coverage"] == "blocked" and "OAuth" in result["error"]


@pytest.mark.parametrize("profile,scopes,error", [
    ("other@example.test", "https://mail.google.com/", "身份"),
    ("ray@anchor-filter.com", "https://www.googleapis.com/auth/gmail.send", "scope"),
])
def test_oauth_identity_and_scopes_are_checked(tmp_path, monkeypatch, profile, scopes, error):
    credential = tmp_path / "oauth.json"
    credential.write_text(json.dumps({"token_uri": "https://oauth2.googleapis.com/token",
                                     "client_id": "fake", "client_secret": "fake", "refresh_token": "fake"}))
    monkeypatch.setattr(gmail.requests, "post", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"access_token": "fake", "scope": scopes}))
    monkeypatch.setattr(gmail.Gmail, "get", lambda *a, **kw: {"emailAddress": profile})
    with pytest.raises(gmail.ScanError, match=error):
        gmail.Gmail(credential, "ray@anchor-filter.com")


def test_overlapping_scan_lock_is_rejected(tmp_path):
    with gmail.locked(tmp_path / "scan.lock"):
        with pytest.raises(BlockingIOError):
            with gmail.locked(tmp_path / "scan.lock"):
                pytest.fail("不应允许同箱并发")


def test_wrapper_runs_each_chain_once_and_combines_failure(monkeypatch, capsys):
    checks = load_script("anchor_checks")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="##CRON_SIGNAL:1##\n", returncode=1)
    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks.main() == 1
    assert len(calls) == 3
    assert capsys.readouterr().out.count("##CRON_SIGNAL:") == 1


def test_installed_fabric_daily_isolates_metrics_without_changing_shared_checks(tmp_path):
    if not monitor.SOURCE.exists():
        pytest.skip("仅部署机验收外部共享脚本；CI 无该业务文件")
    tree = ast.parse(monitor.SOURCE.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cmd_daily")
    called = []
    campaigns = [{"_id": cid, "name": "ANCHOR-" + name, "status": "running"}
                 for name, cid in monitor.CAMPAIGNS.items()]
    campaigns.append({"_id": "fabric", "name": "Mixed-Global-01", "status": "running"})
    def stats(campaign, day, cfg):
        called.append(campaign["_id"])
        return dict(id=campaign["_id"], name=campaign["name"], leads=1, reviewed=1, sent=0, bounced=0,
                    replied=0, opened=0, day_sent=0, day_new=0, day_replies=[], win_new=0,
                    win_bounced=0, pace=0, weekend_sends=0)
    env = dict(datetime=monitor.datetime, timedelta=monitor.timedelta, CN=monitor.CN, json=json,
               load_cfg=lambda: {"bounce_window_days": 7, "anchor_alert_threshold": .03,
                                 "alert_threshold": .05, "bounce_min_sample": 50},
               lem=lambda *a: campaigns, campaign_stats=stats, gmail_scan=lambda cfg: {},
               inquiry_rows=lambda: [], day_window=lambda d: (monitor.datetime(2026, 10, 26, tzinfo=monitor.CN),
                                                              monitor.datetime(2026, 10, 27, tzinfo=monitor.CN)),
               AUTO_DIR=tmp_path, METRICS=tmp_path / "metrics.jsonl", signal=lambda *a: None)
    exec(compile(ast.Module(body=[function], type_ignores=[]), "isolated_cmd_daily", "exec"), env)
    env["cmd_daily"](["2026-10-26", "--fabric-only"])
    assert set(called) == {c["_id"] for c in campaigns}
    result = json.loads((tmp_path / "metrics.jsonl").read_text())
    assert list(result["campaigns"]) == ["Mixed-Global-01"]
