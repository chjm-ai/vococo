"""铃铛通知的「事后处理」+ 早间汇总推送(通知存储见 memory/notices.py)。

act():在铃铛里(或点对话里已超时的旧按钮)选了一个选项——
  - 还在等(pending):直接把答案交给正在等的那一轮,跟按时点击完全一样;
  - 已超时(expired):把你的选择补回原会话,让它接着干:
      提问 → 以「(补答)……」作为新消息发回该会话;
      审批 → 按选项发放许可(一次性 / 下一轮都允许 / 存永久规则),再发一条消息让它重试那一步;
      审批选「拒绝」→ 只关掉通知,不打扰会话。
  待拍板(decision)→ 不管状态,都把选择发回所属 Agent 的主会话让它按这个执行。
  后台任务会话走 task_runner.append(原地续跑),网页会话走 web_bridge(等同你在网页里发消息)。

digest_loop():每天免打扰结束那个整点(BG_APPROVAL_QUIET 的结束小时,默认 8 点)检查一次,
自上次汇总以来还有没处理的通知 → 发一条系统推送「有 N 件事等你处理」,点开直接打开铃铛。
同一时刻每 30 天查一次「永远允许」规则:有 30 天没用过的 → 铃铛里放一条清理提醒(kind=review)。

quick_*():系统通知上的「允许一次 / 拒绝」按钮。Service Worker 拿不到网页登录口令,
所以推送负载里带一个只对这条通知有效的签名(HMAC),点按钮时凭签名调 /notices/quick。
iOS 的网页推送不支持通知按钮,点通知本身会打开铃铛,一点即可。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import time

import anyio

from .. import config
from ..memory import notices

APPROVE_LABELS = ("允许一次", "本次会话都允许", "本轮任务都允许", "永远允许")
DENY_LABEL = "拒绝"


def conv_of(session_key: str) -> str | None:
    """会话 key → Web 端 conv(侧栏/openConv 用的那个名字);不是 Web 能打开的会话返回 None。"""
    if session_key == config.SESSION_KEY:
        return "main"
    if session_key.startswith("web:"):
        return session_key[4:]
    if session_key.startswith(("task:", "voice-chat:")):
        return session_key
    return None


async def resume(session_key: str, text: str) -> str:
    """把一句话作为新消息发回原会话,让它接着干。返回给用户看的一句话结果。"""
    from ..core import task_runner, tasks

    task_id = tasks.task_id_from_session_key(session_key)
    if task_id and tasks.get(task_id) is not None:
        res = await task_runner.append(task_id, text)
        return res.get("message") or "已让任务接着跑"
    conv = conv_of(session_key)
    from . import web_bridge

    if conv is None or not web_bridge.available():
        raise RuntimeError("这个会话没有可续接的网页入口")
    await web_bridge.continue_session(conv, text)
    return "已发回原会话继续"


async def act(notice_id: str, label: str) -> dict:
    """处理一条通知。返回 {"ok": bool, "message"|"error": str, "conv": str|None}。"""
    from . import clarify

    n = notices.get(notice_id)
    if n is None or n["status"] not in notices.OPEN_STATUSES:
        return {"ok": False, "error": "这条已经处理过了"}
    if n["kind"] == "review":
        return _act_review(n, label)
    conv = conv_of(n["session_key"])
    if n["status"] == "pending" and n["clarify_id"] and clarify.resolve(n["clarify_id"], label):
        return {"ok": True, "message": "已回答", "conv": conv}
    if n["options"] and label not in n["options"]:
        return {"ok": False, "error": "选项不存在"}

    if n["kind"] == "approval":
        if label == DENY_LABEL:
            notices.set_status(notice_id, "dismissed", label)
            return {"ok": True, "message": "已拒绝", "conv": conv}
        if label not in APPROVE_LABELS:
            return {"ok": False, "error": "选项不存在"}
        from ..tools import danger

        if label == "永远允许" and n["rule_kind"]:
            from ..memory import approvals

            approvals.add_rule(n["rule_kind"], n["rule_scope"], n["rule_label"] or "")
        elif label in ("本次会话都允许", "本轮任务都允许"):
            danger.grant_round(n["session_key"], n["reason"])
        danger.grant_once(n["session_key"], n["reason"])
        text = (
            f"刚才等批准的操作,主人已批准({label}):{n['reason']}\n{n['detail']}\n"
            "请重新执行这一步,并把之前因此没做完的部分补完。"
        )
    elif n["kind"] == "decision":
        text = (
            f"(拍板)你之前请我拍板:\n{n['prompt']}\n\n我的选择:{label}\n"
            "请按这个执行;要改 GOAL.md / PLAN.md 的照改,做完简短汇报。"
        )
    else:
        text = f"(补答)你之前问:「{n['prompt']}」\n我的回答:{label}\n请接着之前的工作继续。"

    try:
        msg = await resume(n["session_key"], text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"没能发回原会话:{exc}"}
    notices.set_status(notice_id, "answered", label)
    return {"ok": True, "message": msg, "conv": conv}


def label_for_token(n: dict, token: str) -> str | None:
    """旧按钮的 /clarify <id> <序号> → 选项文字。"""
    if token.isdigit() and 0 <= int(token) < len(n["options"]):
        return n["options"][int(token)]
    return None


# ── 系统通知上的快捷按钮 ────────────────────────────────────────────────────
def quick_labels(n: dict) -> list[str]:
    """通知上最多放两个按钮:审批 = 允许一次 + 拒绝;提问 = 前两个选项。"""
    opts = n.get("options") or []
    if n.get("kind") == "approval":
        return [lab for lab in ("允许一次", DENY_LABEL) if lab in opts]
    if n.get("kind") == "ask":
        return opts[:2]
    return []


def _quick_key() -> bytes:
    # 服务端私密值做密钥:VAPID 私钥必然存在(否则根本发不出推送),再混上网页口令
    return f"{config.VAPID_PRIVATE_KEY}|{config.WEB_AUTH_TOKEN}|notice-quick".encode()


def quick_sig(notice_id: str) -> str:
    return hmac.new(_quick_key(), notice_id.encode(), hashlib.sha256).hexdigest()[:24]


async def quick_act(notice_id: str, label: str, sig: str) -> dict:
    """通知按钮点击:验签 + 只允许通知上那两个按钮的选项,其余同 act()。"""
    if not sig or not hmac.compare_digest(quick_sig(notice_id), str(sig)):
        return {"ok": False, "error": "签名不对"}
    n = notices.get(notice_id)
    if n is None or label not in quick_labels(n):
        return {"ok": False, "error": "这条已经处理过了或选项不存在"}
    return await act(notice_id, label)


# ── 早间汇总推送 / 规则清理提醒 ─────────────────────────────────────────────
RULE_STALE_DAYS = 30  # 多久没命中算「可能不需要了」
RULE_REVIEW_EVERY_DAYS = 30  # 多久提醒一次
RULE_KEEP_DAYS = 90  # 点了「都保留」的规则,这么久内不再提
REVIEW_LABELS = ("去清理", "都保留")


def _state_path():
    return config.DATA_DIR / "notice_digest.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def stale_rules(now: float | None = None) -> list[dict]:
    """30 天没命中过的永久规则(从没用过的按创建时间算),排除最近点过「都保留」的。"""
    from ..memory import approvals

    now = now or time.time()
    kept = _load_state().get("kept_rules") or {}
    cut = now - RULE_STALE_DAYS * 86400
    return [
        r for r in approvals.list_rules()
        if (r["last_used_at"] or r["created_at"]) < cut
        and float(kept.get(r["id"], 0)) < now - RULE_KEEP_DAYS * 86400
    ]


def maybe_rule_review(now: datetime.datetime | None = None) -> int:
    """到点且距上次检查满 30 天 → 有久未使用的规则就在铃铛里放一条清理提醒。返回规则条数。"""
    end = _quiet_end_hour()
    now = now or datetime.datetime.now()
    if end is None or now.hour != end:
        return 0
    st = _load_state()
    if now.timestamp() - float(st.get("rule_review_ts") or 0) < RULE_REVIEW_EVERY_DAYS * 86400:
        return 0
    st["rule_review_ts"] = now.timestamp()
    _save_state(st)
    rules = stale_rules(now.timestamp())
    if not rules:
        return 0
    lines = "\n".join(
        f"- {r['kind']} · {r['scope']}(" + ("从没用过" if not r["last_used_at"] else
        f"上次 {datetime.datetime.fromtimestamp(r['last_used_at']):%m-%d}") + ")"
        for r in rules[:10]
    )
    more = f"\n…等共 {len(rules)} 条" if len(rules) > 10 else ""
    notices.add(
        session_key="", kind="review", status="expired", reason="rule_review",
        prompt=f"有 {len(rules)} 条「永远允许」规则 {RULE_STALE_DAYS} 天没用过,不需要的建议删掉:\n{lines}{more}",
        options=list(REVIEW_LABELS), detail=",".join(r["id"] for r in rules),
    )
    return len(rules)


def _act_review(n: dict, label: str) -> dict:
    if label not in REVIEW_LABELS:
        return {"ok": False, "error": "选项不存在"}
    if label == "都保留":
        st = _load_state()
        kept = st.setdefault("kept_rules", {})
        for rid in filter(None, (n["detail"] or "").split(",")):
            kept[rid] = time.time()
        _save_state(st)
    notices.set_status(n["id"], "answered", label)
    return {"ok": True, "message": label, "conv": None, "open": "security" if label == "去清理" else None}


def _quiet_end_hour() -> int | None:
    spec = config.BG_APPROVAL_QUIET
    try:
        return int(spec.split("-", 1)[1]) if spec else None
    except (IndexError, ValueError):
        return None


async def maybe_digest(now: datetime.datetime | None = None) -> int:
    """到点且今天还没发过 → 统计自上次汇总以来没处理的通知,有就推送。返回推送的条数。"""
    end = _quiet_end_hour()
    now = now or datetime.datetime.now()
    if end is None or now.hour != end:
        return 0
    today = now.strftime("%Y-%m-%d")
    st = _load_state()
    if st.get("last_day") == today:
        return 0
    since = float(st.get("last_ts") or (now.timestamp() - 86400))
    n = notices.count_open_since(since)
    st.update(last_day=today, last_ts=now.timestamp())
    _save_state(st)
    if n:
        from .adapters.web_push import PUSH

        await PUSH.notify(
            title="有事等你处理", body=f"夜里攒了 {n} 件事等你处理(审批/提问/拍板),点开铃铛看看",
            conv="main", kind="approval", url="/?notices=1",
        )
    return n


async def digest_loop() -> None:
    while True:
        try:
            maybe_rule_review()  # 先放清理提醒,紧接着的汇总推送会把它算进去
            await maybe_digest()
        except Exception as exc:  # noqa: BLE001
            print(f"[通知汇总] 出错:{exc}", flush=True)
        await anyio.sleep(60)


def startup() -> None:
    """进程启动:上个进程里还在等的选项已经没人接了,统一转成已超时(仍可在铃铛里处理)。"""
    try:
        n = notices.expire_all_pending()
        if n:
            print(f"🔔 {n} 条等待中的选项因重启转为已超时,可在铃铛里继续处理", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[通知] 启动整理失败:{exc}", flush=True)
