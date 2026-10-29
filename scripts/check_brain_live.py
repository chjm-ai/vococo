"""经明确授权才运行的公网 MCP 验收；口令和 OAuth token 只从私有文件读取/保存。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ORIGIN = "https://mcp.chjm.cc"
REDIRECT = "http://127.0.0.1:9899/callback"


def private_read(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("凭据文件必须属于当前用户、非软链，且其他用户无权限")
    return path.read_text().strip()


def private_save(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream)
    temporary.replace(path)


async def authorize(http, metadata, password_file):
    response = await http.post(metadata["registration_endpoint"], json={
        "client_name": "vococo-production-selfcheck", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "offline_access",
    })
    if response.status_code not in (200, 201):
        raise ValueError("OAuth 客户端注册失败，HTTP " + str(response.status_code))
    client_id = response.json()["client_id"]
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = await http.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": "offline_access", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": ORIGIN + "/mcp",
    })
    await reach_login(http, response)
    response = await http.post(ORIGIN + "/.auth/login", data={"password": private_read(password_file)})
    code = await consent(http, response, state)
    response = await http.post(metadata["token_endpoint"], data={"grant_type": "authorization_code",
        "client_id": client_id, "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier})
    if response.status_code != 200:
        raise ValueError("OAuth 换取 token 失败，HTTP " + str(response.status_code))
    return {**response.json(), "client_id": client_id}


async def reach_login(http, response):
    for _ in range(5):
        if response.status_code not in (302, 303):
            raise ValueError("授权请求未重定向至登录")
        location = response.headers["location"]
        if "/.auth/login" in location:
            return
        if not location.startswith("/.idp/auth/"):
            raise ValueError("意外的授权重定向；不跟随第三方地址")
        response = await http.get(ORIGIN + location)
    raise ValueError("登录重定向过多")


async def consent(http, response, state):
    for _ in range(6):
        if response.status_code == 200 and response.url.path.startswith("/.idp/auth/"):
            response = await http.post(response.url)
        if response.status_code not in (302, 303):
            raise ValueError("登录/授权失败，HTTP " + str(response.status_code))
        location = response.headers["location"]
        if location.startswith(REDIRECT + "?"):
            result = parse_qs(urlsplit(location).query)
            if result.get("state") != [state] or "code" not in result:
                raise ValueError("OAuth 回调校验失败")
            return result["code"][0]
        if not location.startswith("/.idp/auth/"):
            raise ValueError("意外的登录重定向；不跟随第三方地址")
        response = await http.get(ORIGIN + location)
    raise ValueError("授权重定向过多")


async def call(session, name, arguments):
    response = await session.call_tool(name, arguments)
    if response.is_error or not isinstance(response.structured_content, dict):
        raise ValueError("MCP 工具调用失败：" + name)
    return response.structured_content


async def wait_terminal(session, task_id):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        task = await call(session, "vococo_get_task", {"id": task_id})
        if task["status"] in {"done", "failed", "cancelled"}:
            return task
        await asyncio.sleep(2)
    raise ValueError("任务未在180秒内进入终态，任务ID=" + task_id)


async def check_tasks(session):
    pending = set()
    try:
        return await check_task_chain(session, pending)
    finally:
        for task_id in pending:
            try:
                await call(session, "vococo_cancel_task", {"id": task_id})
            except Exception:
                print("需手动检查并取消自测任务：" + task_id, flush=True)


async def check_task_chain(session, pending):
    marker = "MCP_LIVE_OK_" + secrets.token_hex(6)
    created = await call(session, "vococo_create_task", {"title": "MCP 上线端到端自测",
        "prompt": "这是用户明确授权的上线自测。不要调用任何工具，不读写文件、不创建子任务。只回复这段验收标记：" + marker,
        "idempotency_key": "deployment-" + marker})
    pending.add(created["id"])
    print("自测任务已创建：" + created["id"], flush=True)
    done = await wait_terminal(session, created["id"])
    pending.discard(created["id"])
    if done["status"] != "done" or marker not in done["result"]:
        raise ValueError("真实任务没有返回验收标记，任务ID=" + created["id"])
    cancelled = await call(session, "vococo_create_task", {"title": "MCP 取消链路自测",
        "prompt": "这是用户授权的取消链路自测。不要读写文件，不调用工具。此任务将被立即取消。",
        "idempotency_key": "cancel-" + marker})
    pending.add(cancelled["id"])
    print("取消测试任务已创建：" + cancelled["id"], flush=True)
    stop = await call(session, "vococo_cancel_task", {"id": cancelled["id"]})
    terminal = await wait_terminal(session, cancelled["id"])
    if not stop["cancel_requested"] or terminal["status"] != "cancelled":
        raise ValueError("取消链路未达到 cancelled，任务ID=" + cancelled["id"])
    history = await call(session, "vococo_read_session", {"key": "task:" + created["id"]})
    if history.get("turn") is None:
        raise ValueError("自测任务历史不可读取")
    return {"created_task": created["id"], "status": done["status"], "marker": marker,
            "cancelled_task": cancelled["id"], "cancel_status": terminal["status"],
            "history_turn": history["turn"]["id"], "receipts": [created["receipt"], done["receipt"], stop["receipt"]]}


async def collect_pages(session, tool, field, arguments):
    items, offset = [], 0
    while True:
        page = await call(session, tool, {**arguments, "offset": offset, "limit": 2})
        items.extend(page[field])
        next_offset = page["next_offset"]
        if next_offset is None:
            return items
        if next_offset <= offset:
            raise ValueError("列表分页未前进")
        offset = next_offset


async def check_history(session, agent_id):
    """只读验收:不派单、不回传历史正文,不把历史当作指令。"""
    agents = await collect_pages(session, "vococo_list_agents", "agents", {})
    target = next((item for item in agents if item["agent_id"] == agent_id), None)
    if not target:
        raise ValueError("目标 Agent 不可见")
    rows = await collect_pages(session, "vococo_list_sessions", "sessions", {"agent_id": agent_id})
    if not rows or any(row["agent_id"] != agent_id for row in rows):
        raise ValueError("目标 Agent 没有会话或归属不匹配")
    chosen = next((row for row in rows if "jll" in row["title"].lower()), rows[0])
    args = {"agent_id": agent_id, "key": chosen["key"], "length": 1000}
    characters, pages = 0, 0
    while True:
        page = await call(session, "vococo_read_session", args)
        if page["turn"] is None:
            raise ValueError("列表中的会话未能读取")
        pages += 1
        characters += len(page["turn"]["user_text"]) + len(page["turn"]["assistant_text"])
        if page["next_offset"] is None:
            break
        args.update(turn_id=page["turn"]["id"], offset=page["next_offset"])
    return {"agent": target, "visible_agents": len(agents), "session_count": len(rows),
            "session": chosen, "read_turn_id": page["turn"]["id"], "read_pages": pages,
            "read_characters": characters, "read_receipt": page["receipt"], "history_read_only": True}


async def check_rate(http, password_file):
    statuses = []
    for _ in range(12):
        response = await http.post(ORIGIN + "/.auth/login", data={"password": "invalid-deployment-test"})
        statuses.append(response.status_code)
        if response.status_code == 429:
            break
    if 429 not in statuses:
        raise ValueError("边缘登录限流未实测触发；HTTP序列=" + str(statuses))
    await asyncio.sleep(12)
    http.cookies.clear()
    response = await http.post(ORIGIN + "/.auth/login", data={"password": private_read(password_file)})
    if response.status_code not in (302, 303):
        raise ValueError("限流等待后正确口令仍不能登录")
    return {"login_post_statuses": statuses, "correct_login_after_wait": response.status_code}


async def run(args):
    async with httpx.AsyncClient(trust_env=args.use_env_proxy, follow_redirects=False, timeout=30) as http:
        discovery = await http.get(ORIGIN + "/.well-known/oauth-authorization-server")
        if discovery.status_code != 200:
            raise ValueError("公网 OAuth 发现失败")
        metadata = discovery.json()
        if metadata["issuer"].rstrip("/") != ORIGIN:
            raise ValueError("issuer 错误")
        auth = await authorize(http, metadata, Path(args.password_file))
        private_save(Path(args.token_file), auth)
        async with httpx2.AsyncClient(trust_env=args.use_env_proxy, timeout=30,
                                      headers={"Authorization": "Bearer " + auth["access_token"]}) as mcp_http:
            async with streamable_http_client(ORIGIN + "/mcp", http_client=mcp_http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    names = [tool.name for tool in (await session.list_tools()).tools]
                    agents = await call(session, "vococo_list_agents", {"limit": 1})
                    result = {"url": ORIGIN + "/mcp", "oauth_login": "passed", "tools": names,
                              "protocol_version": initialized.protocol_version,
                              "read_only_call": "passed", "visible_agent_sample": len(agents["agents"])}
                    if args.history_agent_id:
                        result.update(await check_history(session, args.history_agent_id))
                    elif not args.auth_only:
                        result.update(await check_tasks(session))
        if args.check_rate_limit:
            result["rate_limit"] = await check_rate(http, Path(args.password_file))
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--auth-only", action="store_true")
    parser.add_argument("--history-agent-id", help="只验收该 Agent 的历史读取与分页,不创建模型任务")
    parser.add_argument("--check-rate-limit", action="store_true")
    parser.add_argument("--use-env-proxy", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        # 认证异常或 ExceptionGroup 可能携带请求详情，默认不打印 token/口令上下文。
        raise SystemExit("公网验收失败；未输出底层请求/凭据。请分层检查 OAuth/任务状态。") from None


if __name__ == "__main__":
    main()
