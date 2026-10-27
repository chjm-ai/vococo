#!/usr/bin/env python3
"""陶纤旧扫描的仓库受控接替；原状态、60 秒重叠和首次不追历史保持不变。"""
import argparse
import json
import time
from pathlib import Path

from anchor_gmail_scan import (Gmail, ScanError, classify, forward_id, forward_raw,
                               locked, save, message_summary)

ADDRESS = "ray.chen@anchor-ceramicsfiber.com"
CREDENTIAL = Path.home() / ".gmail-raychen.json"
STATE = Path.home() / "scripts/.raychen_scan_state.json"
TERMINAL = {"sent", "excluded", "manual"}


def load_state(path, now):
    if not path.exists():
        return {"last_ts": now, "forwarded_ids": [], "pending": [], "delivery": {}}
    state = json.loads(path.read_text())
    if (not isinstance(state.get("last_ts"), (int, float)) or
            not isinstance(state.get("forwarded_ids"), list) or
            not isinstance(state.get("pending"), list)):
        raise ScanError("陶纤状态损坏，拒绝重置或扫描历史")
    state.setdefault("delivery", {})
    for pending in state["pending"]:
        # 旧版 False 同时包含明确失败与发送后超时，不能把它当成已确认未发。
        state["delivery"].setdefault(pending["mid"], "legacy_uncertain")
        if state["delivery"][pending["mid"]] == "legacy_uncertain":
            state.setdefault("details", {}).setdefault(pending["mid"], {
                "from": pending.get("frm", ""), "subject": pending.get("subj", ""),
                "note": "旧版 pending 的发送结果无法确认，需人工核对"})
    return state


def process(client, mid, state, path):
    status = state["delivery"][mid]
    marker = forward_id(client.address, mid)
    if status == "legacy_uncertain":
        raise ScanError(f"{mid} 旧版 pending 无发送凭证，保留待人工核对，不自动重发")
    if status in ("sending", "uncertain"):
        if client.ids(f"in:sent rfc822msgid:{marker}"):
            return "sent"
        raise ScanError(f"{mid} 转发结果不明，待人工核对，未重复发送")
    message = client.get(f"/messages/{mid}", format="full")
    state.setdefault("details", {})[mid] = message_summary(message)
    decision = classify(client, message)
    if decision != "customer":
        return decision
    raw = forward_raw(client, mid, marker)
    state["delivery"][mid] = "sending"
    save(path, state)
    return client.send(raw)


def process_candidates(client, ids, state, path):
    errors, submitted = [], []
    for mid in ids:
        try:
            status = process(client, mid, state, path)
            state["delivery"][mid] = status
            if status == "sent":
                state["forwarded_ids"].append(mid)
                submitted.append(mid)
        except Exception as exc:
            errors.append(f"{mid}: {str(exc) if isinstance(exc, ScanError) else type(exc).__name__}")
        save(path, state)
    return errors, submitted


def scan(client, path, now):
    state = load_state(path, now)
    ids = client.ids(f"in:inbox after:{int(state['last_ts']) - 60}") if path.exists() else []
    pending = [p["mid"] for p in state["pending"]]
    candidates = [mid for mid in dict.fromkeys(ids + pending)
                  if mid not in state["forwarded_ids"] and state["delivery"].get(mid) not in TERMINAL]
    state["pending"] = [{"mid": mid} for mid in candidates]
    for mid in candidates:
        state["delivery"].setdefault(mid, "retry")
    save(path, state)  # 元数据失败也必须留下候选，下轮不依赖重叠窗口。
    errors, submitted = process_candidates(client, candidates, state, path)
    state["pending"] = [{"mid": mid} for mid in candidates if state["delivery"][mid] not in TERMINAL]
    state["last_ts"] = now
    save(path, state)
    manual = [mid for mid, status in state["delivery"].items() if status in ("manual", "legacy_uncertain")]
    return {"coverage": "partial" if errors or state["pending"] or manual else "ok",
            "submitted_forward_ids": submitted, "manual_review_ids": manual,
            "submitted_forwards": {mid: state.get("details", {}).get(mid, {}) for mid in submitted},
            "manual_review": {mid: state.get("details", {}).get(mid, {}) for mid in manual},
            "pending": state["pending"], "errors": errors, "last_ts": now}


def read_only(client, path):
    state = load_state(path, int(time.time()))
    ids = client.ids(f"in:inbox after:{int(state['last_ts']) - 60}") if path.exists() else []
    return {"coverage": "read_only", "address": client.address, "messages": len(ids),
            "state_exists": path.exists(), "last_ts": state["last_ts"],
            "pending_count": len(state["pending"]), "forwarding_enabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="沿用旧定时入口参数，允许实际转发")
    args = parser.parse_args()
    try:
        client = Gmail(CREDENTIAL, ADDRESS)
        if args.scan:
            from anchor_gmail_scan import SEND_SCOPES
            if not client.scopes & SEND_SCOPES:
                raise ScanError("未确认转发 scope，拒绝运行")
            with locked(STATE.with_suffix(".lock")):
                result = scan(client, STATE, int(time.time()))
        else:
            result = read_only(client, STATE)
    except Exception as exc:
        result = {"coverage": "blocked", "error": str(exc) if isinstance(exc, ScanError) else type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = result["coverage"] not in ("ok", "read_only")
    print(f"##CRON_SIGNAL:{int(failed or bool(result.get('submitted_forward_ids')))}##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
