#!/usr/bin/env python3
"""lemlist 精简 MCP server(零依赖,stdio JSON-RPC 逐行协议)。

官方 lemlist MCP 注册 120 个工具,schema ≈25 万字符(≈11 万 token/轮),日常
挂载纯浪费。本 server 只暴露外贸拓客常用的 22 个工具(搜客/查活动/查收件箱/
发信),schema 压到 ~2 万 token,是 vococo「外贸工具包」的默认挂载件。

用法(由 vococo external_mcp 配置拉起):
    LEMLIST_API_KEY=<key> python3 lemlist_lite.py

认证:lemlist REST API 用 HTTP Basic,用户名空、密码=API key
(base64(":" + key));User-Agent 必须是浏览器 UA,否则被 Cloudflare 403。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.lemlist.com/api"
KEY = os.environ.get("LEMLIST_API_KEY", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_PROTOCOL = "2025-03-26"


def _call(method: str, path: str, body: dict | None = None) -> dict:
    """调 lemlist REST API,返回 {ok, status, data}。data 为解析后的 JSON 或原样字符串。"""
    headers = {
        "Authorization": "Basic " + base64.b64encode((":" + KEY).encode()).decode(),
        "Accept": "application/json",
        "User-Agent": UA,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                return {"ok": True, "status": resp.status, "data": json.loads(raw)}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "data": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return {"ok": False, "status": e.code, "data": json.loads(raw)}
        except json.JSONDecodeError:
            return {"ok": False, "status": e.code, "data": raw}
    except Exception as e:  # 网络错误等
        return {"ok": False, "status": 0, "data": str(e)}


def _fmt(r: dict) -> dict:
    """把 _call 结果转 MCP 文本 content。"""
    if r["ok"]:
        text = json.dumps(r["data"], ensure_ascii=False)[:8000]
        return {"content": [{"type": "text", "text": text}]}
    return {
        "content": [{"type": "text", "text": f"lemlist API 错误 HTTP {r['status']}: {json.dumps(r['data'], ensure_ascii=False)[:500]}"}],
        "isError": True,
    }


def _str(args, k, default=""):
    v = args.get(k)
    return str(v).strip() if v is not None else default


# 工具表:name -> (描述, inputSchema, handler)
TOOLS: dict[str, tuple[str, dict, object]] = {}


def _reg(name: str, description: str, schema: dict):
    def deco(fn):
        TOOLS[name] = (description, schema, fn)
        return fn
    return deco


@_reg("list_campaigns", "列出团队全部 campaign(邮件营销活动),可按名字搜索。", {
    "type": "object",
    "properties": {"search": {"type": "string", "description": "按活动名模糊搜索"}, "limit": {"type": "integer", "description": "最多返回条数,默认 20"}},
})
def _list_campaigns(args):
    limit = int(args.get("limit") or 20)
    q = f"?limit={limit}"
    if _str(args, "search"):
        q += f"&search={urllib.request.quote(_str(args, 'search'))}"
    return _fmt(_call("GET", "/campaigns" + q))


@_reg("get_campaign", "按 id 获取单个 campaign 详情。", {
    "type": "object", "required": ["campaignId"],
    "properties": {"campaignId": {"type": "string", "description": "cam_ 开头的活动 id"}},
})
def _get_campaign(args):
    return _fmt(_call("GET", f"/campaigns/{_str(args, 'campaignId')}"))


@_reg("get_campaign_leads", "列出某 campaign 里的全部线索(leads),可按 state 过滤(如 replied)。", {
    "type": "object", "required": ["campaignId"],
    "properties": {
        "campaignId": {"type": "string"},
        "state": {"type": "string", "description": "过滤状态,如 reviewed/replied/paused/notInterested,省略=全部"},
        "limit": {"type": "integer", "description": "默认 50,最大 200"},
    },
})
def _get_campaign_leads(args):
    limit = min(int(args.get("limit") or 50), 200)
    q = f"?limit={limit}"
    if _str(args, "state"):
        q += f"&state={_str(args, 'state')}"
    return _fmt(_call("GET", f"/campaigns/{_str(args, 'campaignId')}/leads{q}"))


@_reg("get_campaign_sequences", "查看 campaign 的邮件序列步骤(第几步发什么、间隔几天)。", {
    "type": "object", "required": ["campaignId"],
    "properties": {"campaignId": {"type": "string"}},
})
def _get_campaign_sequences(args):
    return _fmt(_call("GET", f"/campaigns/{_str(args, 'campaignId')}/sequences"))


@_reg("add_campaign_lead", "【写操作·需批准】往 campaign 加一个线索(email 必填),可带姓名/公司等。", {
    "type": "object", "required": ["campaignId", "email"],
    "properties": {
        "campaignId": {"type": "string"},
        "email": {"type": "string"},
        "firstName": {"type": "string"}, "lastName": {"type": "string"},
        "companyName": {"type": "string"}, "jobTitle": {"type": "string"},
        "linkedinUrl": {"type": "string"},
    },
})
def _add_campaign_lead(args):
    body = {"email": _str(args, "email")}
    for k in ("firstName", "lastName", "companyName", "jobTitle", "linkedinUrl"):
        v = _str(args, k)
        if v:
            body[k] = v
    return _fmt(_call("POST", f"/campaigns/{_str(args, 'campaignId')}/leads", body))


@_reg("delete_campaign_lead", "【写操作·需批准】从 campaign 移除一个线索(不影响联系人)。", {
    "type": "object", "required": ["campaignId", "leadId"],
    "properties": {"campaignId": {"type": "string"}, "leadId": {"type": "string", "description": "lea_ 开头"}},
})
def _delete_campaign_lead(args):
    return _fmt(_call("DELETE", f"/campaigns/{_str(args, 'campaignId')}/leads/{_str(args, 'leadId')}"))


@_reg("search_contacts", "按姓名/邮箱搜索 CRM 联系人(团队内已有客户)。", {
    "type": "object",
    "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "limit": {"type": "integer", "description": "默认 20"}},
})
def _search_contacts(args):
    q = f"?limit={min(int(args.get('limit') or 20), 100)}"
    if _str(args, "name"):
        q += f"&name={urllib.request.quote(_str(args, 'name'))}"
    if _str(args, "email"):
        q += f"&email={urllib.request.quote(_str(args, 'email'))}"
    return _fmt(_call("GET", "/contacts" + q))


@_reg("get_contact", "按 id 或邮箱获取单个联系人详情。", {
    "type": "object", "required": ["idOrEmail"],
    "properties": {"idOrEmail": {"type": "string", "description": "ctc_ id 或邮箱"}},
})
def _get_contact(args):
    return _fmt(_call("GET", f"/contacts/{_str(args, 'idOrEmail')}"))


@_reg("get_contact_lists", "列出 CRM 联系人静态列表(客户分组)。", {"type": "object", "properties": {}})
def _get_contact_lists(args):
    return _fmt(_call("GET", "/contacts/lists"))


@_reg("upsert_contact", "【写操作·需批准】创建或更新一个联系人(按邮箱去重,已存在则更新)。", {
    "type": "object",
    "properties": {
        "email": {"type": "string"}, "firstName": {"type": "string"}, "lastName": {"type": "string"},
        "jobTitle": {"type": "string"}, "companyName": {"type": "string"}, "phone": {"type": "string"},
        "linkedinUrl": {"type": "string"},
    },
})
def _upsert_contact(args):
    body = {k: _str(args, k) for k in ("email", "firstName", "lastName", "jobTitle", "companyName", "phone", "linkedinUrl") if _str(args, k)}
    if not body.get("email"):
        return {"content": [{"type": "text", "text": "email 必填"}], "isError": True}
    return _fmt(_call("POST", "/contacts", body))


@_reg("delete_contact", "【写操作·需批准】删除联系人(id 或邮箱),级联删除其 leads/会话等,不可恢复。", {
    "type": "object", "required": ["idOrEmail"],
    "properties": {"idOrEmail": {"type": "string"}},
})
def _delete_contact(args):
    return _fmt(_call("DELETE", f"/contacts/{_str(args, 'idOrEmail')}"))


@_reg("search_lead_by_email", "按邮箱查某线索(lead)在哪个 campaign 及当前状态。", {
    "type": "object", "required": ["email"],
    "properties": {"email": {"type": "string"}},
})
def _search_lead_by_email(args):
    return _fmt(_call("GET", f"/leads?email={urllib.request.quote(_str(args, 'email'))}"))


@_reg("search_people", "搜索 People Database(6 亿+ B2B 联系人库)找潜在客户。API 要求 filters 至少一个带值(如 [{filterId:'country',in:['Vietnam']}]),可叠加 search 自由文本收窄;筛选项见 get_people_filters。", {
    "type": "object",
    "properties": {
        "search": {"type": "string", "description": "自由文本:职位/公司/地点等,如 'sourcing manager textile'"},
        "filters": {"type": "array", "description": "筛选器数组 [{filterId,in:[...],out:[...]}],至少一个 in 带值;先用 get_people_filters 看可用项", "items": {"type": "object"}},
        "page": {"type": "integer", "description": "页码,默认 1"}, "size": {"type": "integer", "description": "每页条数,默认 10,最大 100"},
    },
})
def _search_people(args):
    body = {}
    if _str(args, "search"):
        body["search"] = _str(args, "search")
    # API 要求 filters 必填(可空数组),纯自由文本搜索时传 []
    body["filters"] = args.get("filters") or []
    body["page"] = int(args.get("page") or 1)
    body["size"] = min(int(args.get("size") or 10), 100)
    return _fmt(_call("POST", "/database/people", body))


@_reg("get_people_filters", "People Database 可用的筛选器清单(国家/职位/公司规模等,search_people 用)。", {"type": "object", "properties": {}})
def _get_people_filters(args):
    return _fmt(_call("GET", "/database/filters"))


@_reg("get_inbox_conversations", "列出收件箱会话(与客户的往来邮件),缺 userId 时自动取团队第一个成员。", {
    "type": "object",
    "properties": {
        "userId": {"type": "string", "description": "usr_ id,省略自动取"},
        "limit": {"type": "integer", "description": "默认 20"},
    },
})
def _get_inbox_conversations(args):
    uid = _str(args, "userId")
    if not uid:
        r = _call("GET", "/team")
        if r["ok"] and isinstance(r["data"], dict) and r["data"].get("userIds"):
            uid = r["data"]["userIds"][0]
    q = f"?limit={min(int(args.get('limit') or 20), 100)}"
    if uid:
        q += f"&userId={uid}"
    return _fmt(_call("GET", "/inbox" + q))


@_reg("get_inbox_conversation", "查看与某个联系人的完整往来消息(邮件内容/主题/收发时间)。", {
    "type": "object", "required": ["contactId"],
    "properties": {"contactId": {"type": "string", "description": "ctc_ id"}, "limit": {"type": "integer", "description": "默认 20"}},
})
def _get_inbox_conversation(args):
    q = f"?limit={min(int(args.get('limit') or 20), 100)}"
    return _fmt(_call("GET", f"/inbox/{_str(args, 'contactId')}{q}"))


@_reg("send_email", "【写操作·需批准·真实发信】给联系人发邮件(或回复现有线程)。subject 省略=回复线程。", {
    "type": "object", "required": ["sendUserId", "sendUserEmail", "sendUserMailboxId", "message"],
    "properties": {
        "sendUserId": {"type": "string", "description": "发送者 usr_ id(get_user_channels 或 get_user 可查)"},
        "sendUserEmail": {"type": "string", "description": "发送者邮箱"},
        "sendUserMailboxId": {"type": "string", "description": "发送邮箱账户 usm_ id"},
        "message": {"type": "string", "description": "邮件正文(纯文本即可,自动转 HTML)"},
        "subject": {"type": "string", "description": "主题;回复线程时省略"},
        "contactId": {"type": "string", "description": "收件联系人 ctc_ id(与 leadId 二选一)"},
        "leadId": {"type": "string", "description": "收件线索 lea_ id(与 contactId 二选一)"},
        "replyToActivityId": {"type": "string", "description": "回复某条邮件:传 act_ id 或 latest"},
    },
})
def _send_email(args):
    body = {k: args.get(k) for k in ("sendUserId", "sendUserEmail", "sendUserMailboxId", "message", "subject", "contactId", "leadId", "replyToActivityId") if args.get(k)}
    if not body.get("contactId") and not body.get("leadId"):
        return {"content": [{"type": "text", "text": "contactId 或 leadId 必填"}], "isError": True}
    return _fmt(_call("POST", "/inbox/email", body))


@_reg("list_unsubscribes", "列出退订名单(被退订的邮箱/域名),发信前可查。", {"type": "object", "properties": {}})
def _list_unsubscribes(args):
    return _fmt(_call("GET", "/unsubscribes"))


@_reg("get_activities", "查询活动记录(发信/打开/点击/回复等),看最近动态。", {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "活动类型,如 emailsSent/emailsOpened/emailsReplied,省略=全部"},
        "campaignId": {"type": "string"}, "limit": {"type": "integer", "description": "默认 20,最大 100"},
    },
})
def _get_activities(args):
    q = f"?limit={min(int(args.get('limit') or 20), 100)}"
    if _str(args, "type"):
        q += f"&type={_str(args, 'type')}"
    if _str(args, "campaignId"):
        q += f"&campaignId={_str(args, 'campaignId')}"
    return _fmt(_call("GET", "/activities" + q))


@_reg("get_team", "团队信息(团队 id/成员)。", {"type": "object", "properties": {}})
def _get_team(args):
    return _fmt(_call("GET", "/team"))


@_reg("get_team_credits", "团队剩余点数(积分),做 enrich 前先查够不够。", {"type": "object", "properties": {}})
def _get_team_credits(args):
    return _fmt(_call("GET", "/team/credits"))


@_reg("get_user_channels", "当前账号已连接的发送渠道(邮箱账户/LinkedIn/WhatsApp)——发信前先查可用的 mailbox。", {"type": "object", "properties": {}})
def _get_user_channels(args):
    return _fmt(_call("GET", "/user/channels"))


@_reg("get_user", "按 id 查团队成员详情(邮箱/角色)。", {
    "type": "object", "required": ["userId"],
    "properties": {"userId": {"type": "string", "description": "usr_ id"}},
})
def _get_user(args):
    return _fmt(_call("GET", f"/users/{_str(args, 'userId')}"))


def _tools_list():
    return {
        "tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (desc, schema, _) in TOOLS.items()
        ]
    }


def _handle(method: str, params: dict) -> dict:
    if method == "tools/list":
        return _tools_list()
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return {"content": [{"type": "text", "text": f"未知工具:{name}"}], "isError": True}
        try:
            return TOOLS[name][2](args)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"工具执行失败:{e}"}], "isError": True}
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> int:
    if not KEY:
        sys.stderr.write("LEMLIST_API_KEY 未设置\n")
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:  # notification(如 initialized),不回
            continue
        rid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": _PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lemlist-lite", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        else:
            try:
                result = _handle(method, params)
            except Exception as e:
                result = {"error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
