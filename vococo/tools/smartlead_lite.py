#!/usr/bin/env python3
"""Smartlead 精简 MCP server（零依赖，stdio JSON-RPC 逐行协议）。

官方 Smartlead MCP 当前公开的是诊断工具，写入能力走官方 CLI/API。这里直接封装
VocoTrade 最常用的 Campaign、Lead、发件邮箱、收件箱与分析接口，避免把完整 CLI
和全部 API schema 常驻塞进 Agent 上下文。

用法（由 VocoTrade 自动注册为外部 MCP）：
    SMARTLEAD_API_KEY=<key> python3 smartlead_lite.py

认证：Smartlead V1 API 统一使用 api_key 查询参数。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://server.smartlead.ai/api/v1"
KEY = os.environ.get("SMARTLEAD_API_KEY", "")
_PROTOCOL = "2025-03-26"
TOOLS: dict[str, tuple[str, dict, object]] = {}


def _call(method: str, path: str, body: dict | list | None = None, query: dict | None = None) -> dict:
    """调用 Smartlead API，始终把密钥保留在请求参数而不写到工具返回中。"""
    params = {"api_key": KEY, **(query or {})}
    url = API_BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Accept": "application/json", "User-Agent": "VocoTrade-Smartlead/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode(errors="replace")
            return {"ok": True, "status": response.status, "data": json.loads(raw)}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return {"ok": False, "status": exc.code, "data": payload}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": str(exc)}


def _fmt(result: dict) -> dict:
    """裁剪供应商原始响应，避免单次工具结果撑爆上下文。"""
    if result["ok"]:
        text = json.dumps(result["data"], ensure_ascii=False)[:12000]
        return {"content": [{"type": "text", "text": text}]}
    text = json.dumps(result["data"], ensure_ascii=False)[:800]
    return {
        "content": [{"type": "text", "text": f"Smartlead API 错误 HTTP {result['status']}: {text}"}],
        "isError": True,
    }


def _str(args: dict, key: str, default: str = "") -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else default


def _int(args: dict, key: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(args.get(key) or default), maximum))
    except (TypeError, ValueError):
        return default


def _reg(name: str, description: str, schema: dict):
    def deco(fn):
        TOOLS[name] = (description, schema, fn)
        return fn
    return deco


@_reg("list_campaigns", "列出 Smartlead Campaign，可按 clientId 过滤。", {
    "type": "object", "properties": {"clientId": {"type": "integer"}},
})
def _list_campaigns(args: dict) -> dict:
    query = {"client_id": int(args["clientId"])} if args.get("clientId") else None
    return _fmt(_call("GET", "/campaigns/", query=query))


@_reg("get_campaign", "获取单个 Smartlead Campaign 的设置与状态。", {
    "type": "object", "required": ["campaignId"],
    "properties": {"campaignId": {"type": "integer"}},
})
def _get_campaign(args: dict) -> dict:
    return _fmt(_call("GET", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}"))


@_reg("create_campaign", "【写操作·需批准】创建草稿 Campaign；创建后仍需配置序列、邮箱并显式启动。", {
    "type": "object", "required": ["name"],
    "properties": {"name": {"type": "string"}, "clientId": {"type": "integer"}},
})
def _create_campaign(args: dict) -> dict:
    body = {"name": _str(args, "name")}
    if args.get("clientId"):
        body["client_id"] = int(args["clientId"])
    return _fmt(_call("POST", "/campaigns/create", body))


@_reg("set_campaign_status", "【写操作·需批准】启动、暂停或永久停止 Campaign。STOPPED 不可恢复。", {
    "type": "object", "required": ["campaignId", "status"],
    "properties": {
        "campaignId": {"type": "integer"},
        "status": {"type": "string", "enum": ["START", "PAUSED", "STOPPED"]},
    },
})
def _set_campaign_status(args: dict) -> dict:
    return _fmt(_call(
        "POST", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/status",
        {"status": _str(args, "status").upper()},
    ))


@_reg("get_campaign_sequences", "读取 Campaign 邮件序列、主题、正文、延迟和变体。", {
    "type": "object", "required": ["campaignId"],
    "properties": {"campaignId": {"type": "integer"}},
})
def _get_campaign_sequences(args: dict) -> dict:
    return _fmt(_call("GET", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/sequences"))


@_reg("set_campaign_sequences", "【写操作·需批准】覆盖 Campaign 邮件序列。建议先暂停活动；sequences 必须是 Smartlead 官方格式的步骤数组。", {
    "type": "object", "required": ["campaignId", "sequences"],
    "properties": {
        "campaignId": {"type": "integer"},
        "sequences": {"type": "array", "items": {"type": "object"}},
    },
})
def _set_campaign_sequences(args: dict) -> dict:
    return _fmt(_call(
        "POST", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/sequences",
        {"sequences": args.get("sequences") or []},
    ))


@_reg("list_campaign_leads", "分页读取 Campaign Lead，默认 50 条，最多 100 条。", {
    "type": "object", "required": ["campaignId"],
    "properties": {"campaignId": {"type": "integer"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
})
def _list_campaign_leads(args: dict) -> dict:
    query = {"offset": max(0, int(args.get("offset") or 0)), "limit": _int(args, "limit", 50, 100)}
    return _fmt(_call("GET", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/leads", query=query))


@_reg("add_campaign_leads", "【写操作·需批准】批量导入 Lead 到 Campaign（每次最多 400）。每条至少有 email，可带 first_name、company_name、custom_fields。", {
    "type": "object", "required": ["campaignId", "leads"],
    "properties": {
        "campaignId": {"type": "integer"},
        "leads": {"type": "array", "maxItems": 400, "items": {"type": "object"}},
    },
})
def _add_campaign_leads(args: dict) -> dict:
    leads = args.get("leads") or []
    if not isinstance(leads, list) or not leads or len(leads) > 400:
        return {"content": [{"type": "text", "text": "leads 必须是 1-400 条的数组"}], "isError": True}
    return _fmt(_call(
        "POST", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/leads", {"lead_list": leads},
    ))


@_reg("pause_campaign_lead", "【写操作·需批准】暂停某个 Lead 的后续邮件，不删除资料。", {
    "type": "object", "required": ["campaignId", "leadId"],
    "properties": {"campaignId": {"type": "integer"}, "leadId": {"type": "integer"}},
})
def _pause_campaign_lead(args: dict) -> dict:
    campaign_id = _int(args, "campaignId", 0, 2**31)
    lead_id = _int(args, "leadId", 0, 2**31)
    return _fmt(_call("POST", f"/campaigns/{campaign_id}/leads/{lead_id}/pause"))


@_reg("resume_campaign_lead", "【写操作·需批准】恢复已暂停 Lead 的后续邮件；可指定延后天数。", {
    "type": "object", "required": ["campaignId", "leadId"],
    "properties": {"campaignId": {"type": "integer"}, "leadId": {"type": "integer"}, "delayDays": {"type": "integer"}},
})
def _resume_campaign_lead(args: dict) -> dict:
    campaign_id = _int(args, "campaignId", 0, 2**31)
    lead_id = _int(args, "leadId", 0, 2**31)
    body = {"resume_lead_with_delay_days": max(0, int(args.get("delayDays") or 0))}
    return _fmt(_call("POST", f"/campaigns/{campaign_id}/leads/{lead_id}/resume", body))


@_reg("list_email_accounts", "列出已连接的 Smartlead 发件邮箱及其状态。", {
    "type": "object", "properties": {"offset": {"type": "integer"}, "limit": {"type": "integer"}},
})
def _list_email_accounts(args: dict) -> dict:
    query = {"offset": max(0, int(args.get("offset") or 0)), "limit": _int(args, "limit", 50, 100)}
    return _fmt(_call("GET", "/email-accounts/", query=query))


@_reg("get_warmup_stats", "读取一个发件邮箱最近的 Warmup 进箱/垃圾箱统计。", {
    "type": "object", "required": ["emailAccountId"],
    "properties": {"emailAccountId": {"type": "integer"}},
})
def _get_warmup_stats(args: dict) -> dict:
    account_id = _int(args, "emailAccountId", 0, 2**31)
    return _fmt(_call("GET", f"/email-accounts/{account_id}/warmup-stats"))


@_reg("set_warmup", "【写操作·需批准】调整发件邮箱 Warmup。参数必须使用 Smartlead 官方 warmup 设置格式。", {
    "type": "object", "required": ["emailAccountId", "settings"],
    "properties": {"emailAccountId": {"type": "integer"}, "settings": {"type": "object"}},
})
def _set_warmup(args: dict) -> dict:
    account_id = _int(args, "emailAccountId", 0, 2**31)
    return _fmt(_call("POST", f"/email-accounts/{account_id}/warmup", args.get("settings") or {}))


@_reg("list_inbox_replies", "查询 Master Inbox 的已收到回复，可按 Campaign、日期与分类筛选。", {
    "type": "object", "properties": {"filters": {"type": "object"}},
})
def _list_inbox_replies(args: dict) -> dict:
    filters = args.get("filters") or {}
    body = {"offset": 0, "limit": 20, "filters": filters, "sortBy": "REPLY_TIME_DESC"}
    if isinstance(filters, dict):
        body.update({key: filters[key] for key in ("offset", "limit", "sortBy") if key in filters})
        body["filters"] = {key: value for key, value in filters.items() if key not in ("offset", "limit", "sortBy")}
    body["limit"] = _int(body, "limit", 20, 100)
    body["offset"] = max(0, int(body.get("offset") or 0))
    return _fmt(_call("POST", "/master-inbox/inbox-replies", body))


@_reg("reply_email_thread", "【写操作·需批准·真实发信】回复已有邮件线程；不能用于首次冷邮件。", {
    "type": "object", "required": ["campaignId", "emailStatsId", "emailBody"],
    "properties": {
        "campaignId": {"type": "integer"}, "emailStatsId": {"type": "string"},
        "emailBody": {"type": "string"}, "scheduledAt": {"type": "string"},
    },
})
def _reply_email_thread(args: dict) -> dict:
    body = {"email_stats_id": _str(args, "emailStatsId"), "email_body": _str(args, "emailBody"), "add_signature": True}
    if _str(args, "scheduledAt"):
        body["scheduled_at"] = _str(args, "scheduledAt")
    return _fmt(_call("POST", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/reply-email-thread", body))


@_reg("get_campaign_analytics", "读取 Campaign 按日期的发送、打开、回复、退信等统计。", {
    "type": "object", "required": ["campaignId", "startDate", "endDate"],
    "properties": {"campaignId": {"type": "integer"}, "startDate": {"type": "string"}, "endDate": {"type": "string"}},
})
def _get_campaign_analytics(args: dict) -> dict:
    query = {"start_date": _str(args, "startDate"), "end_date": _str(args, "endDate")}
    return _fmt(_call("GET", f"/campaigns/{_int(args, 'campaignId', 0, 2**31)}/analytics-by-date", query=query))


def _tools_list() -> dict:
    return {"tools": [{"name": name, "description": desc, "inputSchema": schema} for name, (desc, schema, _) in TOOLS.items()]}


def _handle(method: str, params: dict) -> dict:
    if method == "tools/list":
        return _tools_list()
    if method != "tools/call":
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    name = params.get("name", "")
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"未知工具:{name}"}], "isError": True}
    try:
        return TOOLS[name][2](params.get("arguments") or {})
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"工具执行失败:{exc}"}], "isError": True}


def main() -> int:
    if not KEY:
        sys.stderr.write("SMARTLEAD_API_KEY 未设置\n")
        return 1
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            continue
        method = message.get("method", "")
        if method == "initialize":
            result = {"protocolVersion": _PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "smartlead-lite", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        else:
            result = _handle(method, message.get("params") or {})
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
