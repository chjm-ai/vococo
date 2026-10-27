"""三箱共享客户来源判定和陶纤兼容状态的无网络验收。"""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import anchor_gmail_scan as gmail
import anchor_ceramic_scan as ceramic


def message(sender="buyer@example.test", subject="Re: ceramic insulation", **headers):
    values = {"From": sender, "Subject": subject, "In-Reply-To": "<campaign@example.test>"} | headers
    return {"internalDate": str((gmail.NOT_BEFORE + 1000) * 1000), "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": k, "value": v} for k, v in values.items()]}}


class Client:
    address = ceramic.ADDRESS
    scopes = gmail.SEND_SCOPES | gmail.READ_SCOPES

    def __init__(self):
        self.incoming = message()
        self.original = message(self.address, "Ceramic insulation", To="buyer@example.test",
                                **{"Message-ID": "<campaign@example.test>"})
        self.original["labelIds"] = ["SENT"]
        self.sent, self.reads = [], []
        self.fail, self.missing, self.list_fail = False, False, False
        self.send_result, self.reconciled = "sent", False

    def ids(self, query):
        if "rfc822msgid:anchor-" in query:
            return ["receipt"] if self.reconciled else []
        if "rfc822msgid:" in query:
            return [] if self.missing else ["outbound"]
        if self.list_fail:
            raise gmail.ScanError("模拟列表失败")
        return ["incident"]

    def get(self, path, **params):
        self.reads.append((path, params))
        if self.fail:
            raise gmail.ScanError("模拟读取失败")
        if params.get("format") == "raw":
            return {"raw": base64.urlsafe_b64encode(b"From: buyer@example.test\r\nSubject: Quote\r\n\r\nYes").decode()}
        return self.original if path.endswith("outbound") else self.incoming

    def send(self, raw):
        self.sent.append(raw)
        return self.send_result


@pytest.mark.parametrize("sender", ["buyer@example.test", "colleague@another.test", "forwarder@buyer.test"])
def test_customer_reference_accepts_changed_contact_without_sender_allowlist(sender):
    client = Client()
    client.incoming = message(sender)
    assert gmail.classify(client, client.incoming) == "customer"
    assert all(params["format"] == "full" for _, params in client.reads)


@pytest.mark.parametrize("headers", [{"In-Reply-To": "", "References": ""},
                                     {"In-Reply-To": "<unknown@example.test>"}])
def test_unknown_source_goes_to_manual_not_inquiry(headers):
    client = Client()
    client.missing = True
    assert gmail.classify(client, message(**headers)) == "manual"


@pytest.mark.parametrize("header", [{"Auto-Submitted": "auto-replied"}, {"List-Id": "bulk"},
                                    {"Precedence": "bulk"}, {"X-Lemwarm-ID": "test"}])
def test_automatic_mail_never_reaches_association(header):
    client = Client()
    assert gmail.classify(client, message(**header)) == "excluded"
    assert not client.reads


def test_removed_warmup_subject_still_excluded_by_referenced_sent_mail():
    client = Client()
    client.original = message(client.address, "storyboard - lemwarmup 0A3GN9JM",
                              **{"Message-ID": "<campaign@example.test>"})
    client.original["labelIds"] = ["SENT"]
    assert gmail.classify(client, client.incoming) == "excluded"


def test_encoded_subject_and_fabric_identity_do_not_bypass_guard():
    client = Client()
    subject = "=?UTF-8?B?" + base64.b64encode(b"Re: storyboard - lemwarmup 0A3GN9JM").decode() + "?="
    assert gmail.classify(client, message(subject=subject)) == "excluded"
    client.original["payload"]["body"] = {"data": base64.urlsafe_b64encode(b"Regards, Wesley Lin / YXX Fabric").decode()}
    assert gmail.classify(client, client.incoming) == "manual"


def test_false_message_id_or_wrong_oauth_sender_cannot_prove_association():
    client = Client()
    client.original["payload"]["headers"].append({"name": "Message-ID", "value": "<wrong@example.test>"})
    assert gmail.classify(client, client.incoming) == "manual"
    client = Client()
    client.original["payload"]["headers"][0]["value"] = "other@anchor-filter.com"
    assert gmail.classify(client, client.incoming) == "manual"


def legacy_state(path, now):
    state = {"last_ts": now - 60, "forwarded_ids": ["already"], "pending": []}
    path.write_text(json.dumps(state))
    return state


def test_ceramic_incident_excluded_and_old_ids_preserved(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    legacy_state(path, now)
    client.incoming = message(subject="Re: reworked storyboard after the walkthrough feedback - lemwarmup 0A3GN9JM")
    result = ceramic.scan(client, path, now)
    assert result["coverage"] == "ok" and not client.sent
    state = json.loads(path.read_text())
    assert state["forwarded_ids"] == ["already"]
    assert state["delivery"]["incident"] == "excluded" and state["last_ts"] == now


def test_ceramic_metadata_retry_survives_watermark_and_deduplicates(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    legacy_state(path, now)
    client.fail = True
    assert ceramic.scan(client, path, now)["coverage"] == "partial"
    assert json.loads(path.read_text())["pending"] == [{"mid": "incident"}]
    client.fail = False
    assert ceramic.scan(client, path, now + 7200)["coverage"] == "ok"
    assert ceramic.scan(client, path, now + 7300)["coverage"] == "ok"
    assert len(client.sent) == 1


def test_ceramic_uncertain_never_resends_and_retry_is_visible(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    legacy_state(path, now)
    client.send_result = "retry"
    assert ceramic.scan(client, path, now)["coverage"] == "partial"
    client.send_result = "uncertain"
    assert ceramic.scan(client, path, now + 1)["coverage"] == "partial"
    assert ceramic.scan(client, path, now + 2)["coverage"] == "partial"
    assert len(client.sent) == 2
    client.reconciled = True
    assert ceramic.scan(client, path, now + 3)["coverage"] == "ok"
    assert len(client.sent) == 2


def test_ceramic_list_failure_and_corrupt_state_do_not_reset(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    legacy_state(path, now)
    previous = path.read_bytes()
    client.list_fail = True
    with pytest.raises(gmail.ScanError):
        ceramic.scan(client, path, now)
    assert path.read_bytes() == previous
    path.write_text("corrupt")
    with pytest.raises(ValueError):
        ceramic.scan(client, path, now)
    assert path.read_text() == "corrupt"


def test_ceramic_first_run_and_read_only_never_backfill_or_mutate(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    assert ceramic.read_only(client, path)["forwarding_enabled"] is False
    assert not path.exists()
    assert ceramic.scan(client, path, now)["coverage"] == "ok"
    assert not client.sent
    before = path.read_bytes()
    ceramic.read_only(client, path)
    assert path.read_bytes() == before


def test_manual_remains_visible_without_automatic_send(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    legacy_state(path, now)
    client.missing = True
    assert ceramic.scan(client, path, now)["manual_review_ids"] == ["incident"]
    assert ceramic.scan(client, path, now + 1)["coverage"] == "partial"
    assert not client.sent


@pytest.mark.parametrize("address", [ceramic.ADDRESS, *gmail.MAILBOXES])
@pytest.mark.parametrize("kind", ["warmup", "unknown", "colleague"])
def test_actual_scanner_path_for_each_mailbox_is_safe(tmp_path, address, kind):
    client, path, now = Client(), tmp_path / "state.json", gmail.NOT_BEFORE + 1000
    client.address = address
    client.original["payload"]["headers"][0]["value"] = address
    if kind == "warmup":
        client.incoming = message(subject="Re: reworked storyboard after the walkthrough feedback - lemwarmup 0A3GN9JM")
    elif kind == "unknown":
        client.missing = True
    else:
        client.incoming = message("new-colleague@another.test")
    if address == ceramic.ADDRESS:
        legacy_state(path, now)
        result = ceramic.scan(client, path, now)
    else:
        result = gmail.scan(client, path, now)
    assert len(client.sent) == int(kind == "colleague")
    assert result["coverage"] == ("partial" if kind == "unknown" else "ok")


def test_second_page_failure_is_not_partial_success():
    client = object.__new__(gmail.Gmail)
    def get(path, **params):
        if params.get("pageToken"):
            raise gmail.ScanError("第二页失败")
        return {"messages": [{"id": "first"}], "nextPageToken": "next"}
    client.get = get
    with pytest.raises(gmail.ScanError, match="第二页失败"):
        client.ids("query")


def test_legacy_pending_without_receipt_remains_manual_not_resent(tmp_path):
    client, path, now = Client(), tmp_path / "legacy.json", gmail.NOT_BEFORE + 1000
    state = legacy_state(path, now)
    state["pending"] = [{"mid": "incident", "frm": "buyer@example.test", "subj": "Re: ceramic"}]
    path.write_text(json.dumps(state))
    for tick in (now, now + 7200):
        result = ceramic.scan(client, path, tick)
        assert result["coverage"] == "partial"
        assert result["manual_review_ids"] == ["incident"]
        assert "旧版 pending" in result["errors"][0]
    assert not client.sent
    assert json.loads(path.read_text())["pending"] == [{"mid": "incident"}]


@pytest.mark.parametrize("order", ["<fabric@example.test> <campaign@example.test>",
                                    "<campaign@example.test> <fabric@example.test>"])
def test_mixed_fabric_and_customer_references_stay_manual(order):
    client = Client()
    fabric = message(client.address, **{"Message-ID": "<fabric@example.test>"})
    fabric["labelIds"] = ["SENT"]
    fabric["payload"]["body"] = {"data": base64.urlsafe_b64encode(b"YXX Fabric").decode()}
    original_get = client.get
    client.ids = lambda query: ["fabric"] if "fabric@example.test" in query else ["outbound"]
    client.get = lambda path, **params: fabric if path.endswith("fabric") else original_get(path, **params)
    assert gmail.classify(client, message(**{"In-Reply-To": "", "References": order})) == "manual"


def test_repeated_gmail_page_fails_instead_of_silent_or_infinite_scan():
    client = object.__new__(gmail.Gmail)
    client.get = lambda *a, **kw: {"messages": [{"id": "same"}], "nextPageToken": "repeat"}
    with pytest.raises(gmail.ScanError, match="重复分页"):
        client.ids("query")
