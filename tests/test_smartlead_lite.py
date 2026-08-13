"""Smartlead 精简 MCP 协议与请求映射测试，不访问真实 Smartlead。"""
from __future__ import annotations

import json

from vococo.tools import smartlead_lite


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_tools_list_keeps_core_operations_compact():
    tools = smartlead_lite._tools_list()["tools"]
    names = {tool["name"] for tool in tools}
    assert {"list_campaigns", "create_campaign", "add_campaign_leads", "reply_email_thread"} <= names
    assert len(names) == 16


def test_add_campaign_leads_uses_official_lead_list_wrapper(monkeypatch):
    captured = {}

    def fake_call(method, path, body=None, query=None):
        captured.update(method=method, path=path, body=body, query=query)
        return {"ok": True, "status": 200, "data": {"ok": True}}

    monkeypatch.setattr(smartlead_lite, "_call", fake_call)
    result = smartlead_lite._add_campaign_leads({
        "campaignId": 123,
        "leads": [{"email": "buyer@example.com", "company_name": "Acme"}],
    })

    assert json.loads(_text(result)) == {"ok": True}
    assert captured == {
        "method": "POST", "path": "/campaigns/123/leads",
        "body": {"lead_list": [{"email": "buyer@example.com", "company_name": "Acme"}]},
        "query": None,
    }


def test_add_campaign_leads_rejects_empty_or_oversized_input():
    assert "1-400" in _text(smartlead_lite._add_campaign_leads({"campaignId": 1, "leads": []}))
    too_many = [{"email": f"buyer{i}@example.com"} for i in range(401)]
    assert "1-400" in _text(smartlead_lite._add_campaign_leads({"campaignId": 1, "leads": too_many}))


def test_list_inbox_replies_builds_paged_official_body(monkeypatch):
    captured = {}

    def fake_call(method, path, body=None, query=None):
        captured.update(method=method, path=path, body=body, query=query)
        return {"ok": True, "status": 200, "data": {"replies": []}}

    monkeypatch.setattr(smartlead_lite, "_call", fake_call)
    smartlead_lite._list_inbox_replies({"filters": {"campaignId": [123], "limit": 50}})

    assert captured == {
        "method": "POST", "path": "/master-inbox/inbox-replies", "query": None,
        "body": {
            "offset": 0, "limit": 50, "sortBy": "REPLY_TIME_DESC",
            "filters": {"campaignId": [123]},
        },
    }


def test_reply_thread_preserves_string_email_stats_id(monkeypatch):
    captured = {}

    def fake_call(method, path, body=None, query=None):
        captured.update(method=method, path=path, body=body, query=query)
        return {"ok": True, "status": 200, "data": {"ok": True}}

    monkeypatch.setattr(smartlead_lite, "_call", fake_call)
    smartlead_lite._reply_email_thread({
        "campaignId": 123, "emailStatsId": "abc-123", "emailBody": "Thanks!",
    })

    assert captured == {
        "method": "POST", "path": "/campaigns/123/reply-email-thread", "query": None,
        "body": {"email_stats_id": "abc-123", "email_body": "Thanks!", "add_signature": True},
    }


def test_call_does_not_include_key_in_tool_result(monkeypatch):
    monkeypatch.setattr(smartlead_lite, "KEY", "secret-key")

    class Response:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(smartlead_lite.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    result = smartlead_lite._list_campaigns({})

    assert "secret-key" not in _text(result)
