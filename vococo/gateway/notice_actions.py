"""铃铛通知的「事后处理」+ 早间汇总推送(通知存储见 memory/notices.py)。

act():在铃铛里(或点对话里已超时的旧按钮)选了一个选项——
  - 还在等(pending):直接把答案交给正在等的那一轮,跟按时点击完全一样;
  - 已超时(expired):把你的选择补回原会话,让它接着干:
      提问 → 以「(补答)……」作为新消息发回该会话;
      审批 → 按选项发放许可(一次性 / 下一轮都允许 / 存永久规则),再发一条消息让它重试那一步;
      审批选「拒绝」→ 只关掉通知,不打扰会话。
  后台任务会话走 task_runner.append(原地续跑),网页会话走 web_bridge(等同你在网页里发消息)。

digest_loop():每天免打扰结束那个整点(BG_APPROVAL_QUIET 的结束小时,默认 8 点)检查一次,
自上次汇总以来还有没处理的通知 → 发一条系统推送「有 N 件事等你处理」,点开直接打开铃铛。
"""
from __future__ import annotations

import datetime
import json

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


# ── 早间汇总推送 ──────────────────────────────────────────────────────────
def _state_path():
    return config.DATA_DIR / "notice_digest.json"


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
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if st.get("last_day") == today:
        return 0
    since = float(st.get("last_ts") or (now.timestamp() - 86400))
    n = notices.count_open_since(since)
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(
        json.dumps({"last_day": today, "last_ts": now.timestamp()}), encoding="utf-8"
    )
    if n:
        from .adapters.web_push import PUSH

        await PUSH.notify(
            title="有事等你处理", body=f"夜里攒了 {n} 件事等你处理(审批/提问),点开铃铛看看",
            conv="main", kind="approval", url="/?notices=1",
        )
    return n


async def digest_loop() -> None:
    while True:
        try:
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
