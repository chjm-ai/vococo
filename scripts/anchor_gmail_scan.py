#!/usr/bin/env python3
"""隔热两新箱独立扫描。默认只读；--apply 才转发；不借用其他箱的 OAuth。"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import re
import time
from collections import Counter
from contextlib import contextmanager
from email import message_from_bytes
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path

import requests

GM = "https://gmail.googleapis.com/gmail/v1/users/me"
DESTINATION = "ray.chen@anchorgd.com"
MAILBOXES = {
    "ray@anchor-filter.com": Path.home() / ".gmail-anchor-filter.json",
    "ray@anchor-microporous.com": Path.home() / ".gmail-anchor-microporous.json",
}
STATE_DIR = Path.home() / ".local/state/anchor-mail"
NOT_BEFORE = 1792994400  # 2026-10-26 06:00 UTC；首次启用再取 max(now, 此下限)
OWN_DOMAINS = {"anchor-filter.com", "anchor-microporous.com", "anchor-ceramicsfiber.com", "anchorgd.com"}
AUTO_SUBJECT = re.compile(r"automatic reply|auto.?reply|out of office|undeliver|delivery status|"
                          r"returned mail|mail delivery|自动回复|réponse autom|respuesta autom|abwesen", re.I)
READ_SCOPES = {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.readonly",
               "https://www.googleapis.com/auth/gmail.modify"}
SEND_SCOPES = {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.send",
               "https://www.googleapis.com/auth/gmail.modify"}


class ScanError(Exception):
    pass


class Gmail:
    def __init__(self, credential, address):
        if not credential.exists():
            raise ScanError("缺该邮箱 OAuth 文件，需本人授权 gmail.readonly + gmail.send")
        cred = json.loads(credential.read_text())
        if cred.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ScanError("OAuth token_uri 不在允许列表")
        response = requests.post(cred["token_uri"], data={
            key: cred[key] for key in ("client_id", "client_secret", "refresh_token")
        } | {"grant_type": "refresh_token"}, timeout=30)
        if response.status_code != 200:
            raise ScanError(f"OAuth 刷新失败 HTTP {response.status_code}")
        token = response.json()
        self.headers = {"Authorization": "Bearer " + token["access_token"]}
        self.scopes = set(token.get("scope", "").split())
        profile = self.get("/profile")
        if profile.get("emailAddress", "").lower() != address:
            raise ScanError("OAuth 邮箱身份不匹配，拒绝扫描和转发")
        self.address = address
        if not self.scopes & READ_SCOPES:
            raise ScanError("未确认 gmail 读取 scope，拒绝扫描")

    def get(self, path, **params):
        response = requests.get(GM + path, headers=self.headers, params=params, timeout=60)
        if response.status_code != 200:
            raise ScanError(f"Gmail 读取失败 HTTP {response.status_code}")
        return response.json()

    def ids(self, query):
        token, found = None, []
        while True:
            params = {"q": query, "maxResults": 100, "includeSpamTrash": True}
            if token:
                params["pageToken"] = token
            page = self.get("/messages", **params)
            found.extend(row["id"] for row in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                return found

    def send(self, raw):
        try:
            response = requests.post(GM + "/messages/send", headers=self.headers,
                                     json={"raw": raw}, timeout=120)
        except requests.RequestException:
            return "uncertain"
        if response.status_code == 200 and response.json().get("id"):
            return "sent"
        # 5xx 和超时可能已被服务器接受；不能直接重发。
        return "retry" if 400 <= response.status_code < 500 else "uncertain"


def headers_of(message):
    return {row["name"].lower(): row["value"] for row in message.get("payload", {}).get("headers", [])}


def is_reply(message):
    headers = headers_of(message)
    address = parseaddr(headers.get("from", ""))[1].lower()
    if not address or "@" not in address or "SENT" in message.get("labelIds", []):
        return False
    if address.rsplit("@", 1)[-1] in OWN_DOMAINS or headers.get("x-anchor-forward"):
        return False
    if any(part in address for part in ("noreply", "no-reply", "mailer-daemon", "postmaster", "dmarc",
                                        "mailinblack", "lemwarm", "lemlist", "notifications@")):
        return False
    if headers.get("auto-submitted", "no").lower() != "no":
        return False
    if headers.get("precedence", "").lower() in ("bulk", "junk", "list") or headers.get("list-id"):
        return False
    if AUTO_SUBJECT.search(headers.get("subject", "")):
        return False
    return bool(headers.get("in-reply-to") or headers.get("references"))


def forward_id(address, mid):
    digest = hashlib.sha256(f"{address}:{mid}".encode()).hexdigest()
    return f"anchor-{digest}@vococo.local"


def forward_raw(client, mid, marker):
    data = client.get(f"/messages/{mid}", format="raw")
    raw = data["raw"]
    original = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    forwarded = MIMEMultipart()
    forwarded["From"], forwarded["To"] = client.address, DESTINATION
    forwarded["Message-ID"], forwarded["X-Anchor-Forward"] = f"<{marker}>", "1"
    forwarded["Subject"] = "Fwd: " + str(original.get("Subject", ""))
    forwarded.attach(MIMEText(f"隔热客户回复；来源邮箱：{client.address}。原信见附件。", "plain", "utf-8"))
    forwarded.attach(MIMEMessage(original))
    return base64.urlsafe_b64encode(forwarded.as_bytes()).decode()


def save(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def process_message(client, mid, state, path, now):
    marker = forward_id(client.address, mid)
    status = state["messages"].get(mid)
    if status in ("sent", "excluded", "old"):
        return status
    if status in ("sending", "uncertain"):
        if client.ids(f"in:sent rfc822msgid:{marker}"):
            state["messages"][mid] = "sent"
            save(path, state)
            return "sent"
        # Gmail 索引有延迟，始终不盲目重发，留给人工核对。
        raise ScanError("转发结果不明：已保留待核对，未重复发送")
    message = client.get(f"/messages/{mid}", format="full")
    if int(message["internalDate"]) / 1000 < state["enabled_at"]:
        state["messages"][mid] = "old"
    elif not is_reply(message):
        state["messages"][mid] = "excluded"
    else:
        raw = forward_raw(client, mid, marker)
        state["messages"][mid] = "sending"
        save(path, state)  # 外发前持久化，进程崩溃也不能无条件重发。
        state["messages"][mid] = client.send(raw)
    save(path, state)
    return state["messages"][mid]


def scan(client, path, now):
    if not client.scopes & SEND_SCOPES:
        raise ScanError("缺转发 scope gmail.send，未启用；读取仍可用")
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("address") != client.address:
            raise ScanError("状态邮箱身份不匹配")
    else:
        state = {"address": client.address, "enabled_at": max(NOT_BEFORE, now),
                 "last_ts": now, "messages": {}}
        save(path, state)
    start = max(state["enabled_at"], state["last_ts"] - 3600)
    ids = client.ids(f"after:{int(start) - 1} before:{int(now) + 1} -in:sent -in:drafts -in:trash")
    pending = [mid for mid, status in state["messages"].items() if status not in ("sent", "excluded", "old")]
    candidates = [mid for mid in dict.fromkeys(ids + pending)
                  if state["messages"].get(mid) not in ("sent", "excluded", "old")]
    # 先持久化整页候选，单封元数据失败也不会因水位推进丢失。
    for mid in candidates:
        state["messages"].setdefault(mid, "retry")
    save(path, state)
    counts, errors = Counter(), []
    for mid in candidates:
        try:
            counts[process_message(client, mid, state, path, now)] += 1
        except Exception as exc:
            errors.append(str(exc) if isinstance(exc, ScanError) else type(exc).__name__)
    state["last_ts"] = now
    save(path, state)
    return {"coverage": "partial" if errors or counts["retry"] or counts["uncertain"] else "ok",
            "counts": dict(counts), "errors": errors, "enabled_at": state["enabled_at"]}


def run_mailbox(address, credential, apply, state_dir=STATE_DIR):
    try:
        client = Gmail(credential, address)
        if not apply:
            ids = client.ids(f"after:{NOT_BEFORE} -in:sent -in:drafts -in:trash")
            return {"coverage": "read_only", "readable": True,
                    "send_scope": bool(client.scopes & SEND_SCOPES), "messages_since_floor": len(ids),
                    "forwarding_enabled": False}
        path = state_dir / (address.replace("@", "_") + ".json")
        with locked(path.with_suffix(".lock")):
            return scan(client, path, int(time.time()))
    except Exception as exc:
        return {"coverage": "blocked", "error": str(exc) if isinstance(exc, ScanError) else type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    results = {address: run_mailbox(address, credential, args.apply)
               for address, credential in MAILBOXES.items()}
    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = any(row["coverage"] not in ("ok", "read_only") for row in results.values())
    forwarded = any(row.get("counts", {}).get("sent", 0) for row in results.values())
    print(f"##CRON_SIGNAL:{int(failed or forwarded)}##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
