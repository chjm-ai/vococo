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
from email.header import decode_header, make_header
from email.utils import getaddresses
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
        token, found, pages = None, [], set()
        while True:
            params = {"q": query, "maxResults": 100, "includeSpamTrash": True}
            if token:
                params["pageToken"] = token
            page = self.get("/messages", **params)
            found.extend(row["id"] for row in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                return list(dict.fromkeys(found))
            if token in pages:
                raise ScanError("Gmail 列表重复分页，拒绝推进水位")
            pages.add(token)

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
    headers = {}
    for row in message.get("payload", {}).get("headers", []):
        name = row["name"].lower()
        value = str(make_header(decode_header(row["value"])))
        headers[name] = headers.get(name, "") + " " + value if name in headers else value
    return headers


def is_warmup(message):
    headers = headers_of(message)
    # 只认产品标记/专用头，不把 warm、warmup、故事板等普通客户用语拉黑。
    if re.search(r"(?:^|\s)-\s*lemwarmup(?:\s+[a-z0-9]{6,})?\s*$", headers.get("subject", ""), re.I):
        return True
    return any(name.startswith(("x-lemwarm", "x-lem-warm")) for name in headers)


def reference_ids(message):
    headers = headers_of(message)
    return list(dict.fromkeys(re.findall(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>",
                                         headers.get("in-reply-to", "") + " " + headers.get("references", ""))))


def payload_text(payload):
    data = payload.get("body", {}).get("data", "")
    text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    return text + "\n" + "\n".join(payload_text(part) for part in payload.get("parts", [])
                                   if not part.get("filename"))


def outbound_source(client, original, reference):
    headers = headers_of(original)
    if "SENT" not in original.get("labelIds", []) or headers.get("message-id") != reference:
        return "manual"
    if is_warmup(original):
        return "excluded"
    sender = parseaddr(headers.get("from", ""))[1].lower()
    recipients = getaddresses([headers.get("to", ""), headers.get("cc", "")])
    external = [addr for _, addr in recipients if "@" in addr and addr.rsplit("@", 1)[-1].lower() not in OWN_DOMAINS]
    if sender != client.address or not external or headers.get("x-anchor-forward"):
        return "identity_conflict"
    # 已发邮件本身仍可能身份串线；不把面料签名的外发认成隔热开发信。
    if re.search(r"yxx\s*fabric|yxxfabric\.", payload_text(original.get("payload", {})), re.I):
        return "identity_conflict"
    return "customer"


def classify(client, message):
    if is_warmup(message) or not is_reply(message, require_reference=False):
        return "excluded"
    related, conflict = False, False
    for reference in reference_ids(message):
        # reference 格式被限制，不能注入 Gmail 查询条件；再核对返回 Message-ID 和 SENT。
        for mid in client.ids(f"in:sent rfc822msgid:{reference[1:-1]}"):
            original = client.get(f"/messages/{mid}", format="full")
            source = outbound_source(client, original, reference)
            if source == "excluded":
                return "excluded"
            related |= source == "customer"
            conflict |= source == "identity_conflict"
    return "customer" if related and not conflict else "manual"


def is_reply(message, require_reference=True):
    if is_warmup(message):
        return False
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
    return not require_reference or bool(reference_ids(message))


def message_summary(message):
    headers = headers_of(message)
    return {"from": headers.get("from", ""), "subject": headers.get("subject", ""),
            "thread_id": message.get("threadId"), "note": "引用关联不证明采购意向"}


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


def backup_state(path):
    backup = path.with_name(f"{path.name}.manual-warmup-{time.time_ns()}.bak")
    with backup.open("xb") as stream:
        backup.chmod(0o600)
        stream.write(path.read_bytes())
    return str(backup)


def reclassify_manual(client, state, path, key):
    # 只复核人工队列，不触碰已发送、旧信或发送结果不明的记录。
    verified, errors = {}, []
    for mid, status in state[key].items():
        if status != "manual":
            continue
        try:
            message = client.get(f"/messages/{mid}", format="full")
            if is_warmup(message):
                verified[mid] = message_summary(message)
        except Exception as exc:
            errors.append(f"{mid}: {str(exc) if isinstance(exc, ScanError) else type(exc).__name__}")
    backup = backup_state(path) if verified else None
    for mid, summary in verified.items():
        state[key][mid] = "excluded"
        state.setdefault("details", {})[mid] = summary
        state.setdefault("manual_reclassifications", {})[mid] = {
            "from": "manual", "to": "excluded", "reason": "lemwarm_product_marker",
            "backup": backup}
    if verified:
        save(path, state)
    return {"reclassified_manual_ids": list(verified), "manual_migration_backup": backup}, errors


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def process_message(client, mid, state, path, now):
    marker = forward_id(client.address, mid)
    status = state["messages"].get(mid)
    if status in ("sent", "excluded", "old", "manual"):
        return status
    if status in ("sending", "uncertain"):
        if client.ids(f"in:sent rfc822msgid:{marker}"):
            state["messages"][mid] = "sent"
            save(path, state)
            return "sent"
        # Gmail 索引有延迟，始终不盲目重发，留给人工核对。
        raise ScanError("转发结果不明：已保留待核对，未重复发送")
    message = client.get(f"/messages/{mid}", format="full")
    state.setdefault("details", {})[mid] = message_summary(message)
    if int(message["internalDate"]) / 1000 < state["enabled_at"]:
        state["messages"][mid] = "old"
    else:
        state["messages"][mid] = classify(client, message)
    if state["messages"][mid] == "customer":
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
    migration, errors = reclassify_manual(client, state, path, "messages")
    pending = [mid for mid, status in state["messages"].items() if status not in ("sent", "excluded", "old", "manual")]
    candidates = [mid for mid in dict.fromkeys(ids + pending)
                  if state["messages"].get(mid) not in ("sent", "excluded", "old", "manual")]
    # 先持久化整页候选，单封元数据失败也不会因水位推进丢失。
    for mid in candidates:
        state["messages"].setdefault(mid, "retry")
    save(path, state)
    counts = Counter()
    for mid in candidates:
        try:
            counts[process_message(client, mid, state, path, now)] += 1
        except Exception as exc:
            errors.append(str(exc) if isinstance(exc, ScanError) else type(exc).__name__)
    state["last_ts"] = now
    save(path, state)
    manual = [mid for mid, status in state["messages"].items() if status == "manual"]
    return {"coverage": "partial" if errors or counts["retry"] or counts["uncertain"] or manual else "ok",
            "counts": dict(counts), "manual_review_ids": manual,
            "manual_review": {mid: state.get("details", {}).get(mid, {}) for mid in manual},
            "errors": errors, "enabled_at": state["enabled_at"], **migration}


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
