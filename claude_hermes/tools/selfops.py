"""自我运维 —— agent 改完自身代码后的「重启 + 还魂验证」闭环。

流程(遗书 + 还魂):
1. agent 调 restart_self 工具(定义在 builtin.py,逻辑在这)→ 预检新代码
   (compileall 全量语法 + 主链 import)→ 写下 data/resume_task.json
   (遗书:对话在哪、验证什么、回滚锚点)→ 标记待重启。
2. 网关在【本轮回复完整结束、历史落库之后】看到标记 → 通知用户 → 进程退出。
   拉起完全交给 deploy/run.sh 守护循环 —— 自己绝不 spawn 新进程,
   保证任何时刻只有一个实例(否则孤儿进程会抢 TG 轮询)。
3. 新进程启动 → GatewayRunner 读到遗书(读完即删,防重复触发)→ 往原对话
   注入一条系统消息,agent 带着 SQLite 里的完整历史继续执行验证计划。

保险丝:
- 预检不过 → 不重启,原地报错(防「重启进坟墓」:语法错则新进程根本起不来)。
- 15 分钟内最多 3 次自我重启,超了拒绝(防「改→挂→再改」无人值守死循环)。
- 工作区有未提交改动默认拒绝(commit 即回滚锚点,锚点必须干净才可靠)。
- 新代码启动即崩 → run.sh 连崩 3 次后按遗书锚点 git reset --hard 回滚,
  并 touch data/.rollback_done;还魂消息据此改为「已回滚,别再重启」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import anyio

from .. import config

_REPO_ROOT = config.DATA_DIR.parent
RESUME_PATH = config.DATA_DIR / "resume_task.json"
RESTART_STAMPS_PATH = config.DATA_DIR / "self_restarts.json"
ROLLBACK_FLAG_PATH = config.DATA_DIR / ".rollback_done"

_RATE_WINDOW_SEC = 15 * 60
_RATE_MAX = 3
_EXIT_CODE = 51  # 区别于崩溃的退出码,run.sh 日志里一眼认出"这是主动重启"

_restart_pending = False


# ── git / 预检(均为阻塞调用,工具侧经 anyio.to_thread 执行)──
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30
    )


def git_head() -> str | None:
    try:
        r = _git("rev-parse", "HEAD")
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_dirty() -> bool:
    """已跟踪文件有未提交改动?(-uno:未跟踪文件不算,data/ 等运行时产物无碍)"""
    try:
        r = _git("status", "--porcelain", "-uno")
        return bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def preflight() -> str | None:
    """新代码健康检查;返回 None=通过,否则错误摘要。

    compileall 抓全量语法错误;import 主链抓 import 期错误(含懒加载的
    web/telegram adapter —— 自我修改最常动的就是它们)。
    """
    checks = [
        (["-m", "compileall", "-q", "claude_hermes"], "语法检查"),
        (
            [
                "-c",
                "import claude_hermes.gateway.run, "
                "claude_hermes.gateway.adapters.web, "
                "claude_hermes.gateway.adapters.telegram",
            ],
            "主链 import",
        ),
    ]
    for argv, label in checks:
        try:
            r = subprocess.run(
                [sys.executable, *argv],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            return f"{label}超时(90s)"
        if r.returncode != 0:
            out = (r.stderr or r.stdout or "").strip()
            return f"{label}失败:\n{out[-1500:]}"
    return None


# ── 频率保险丝 ──
def _recent_restarts() -> list[float]:
    try:
        stamps = json.loads(RESTART_STAMPS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        stamps = []
    now = time.time()
    return [s for s in stamps if isinstance(s, (int, float)) and now - s < _RATE_WINDOW_SEC]


def _record_restart(recent: list[float]) -> None:
    RESTART_STAMPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESTART_STAMPS_PATH.write_text(
        json.dumps(recent + [time.time()]), encoding="utf-8"
    )


# ── 遗书:写(工具侧)──
def request_restart(
    *,
    platform: str,
    chat_id: object,
    session_key: str,
    reason: str,
    verify_plan: str,
    allow_dirty: bool = False,
) -> str:
    """全部保险丝通过则写遗书 + 标记待重启;返回给 agent 的结果文案。"""
    global _restart_pending
    if _restart_pending:
        return "已有一个重启在排队(本轮回复结束即执行),不用重复调用。"
    recent = _recent_restarts()
    if len(recent) >= _RATE_MAX:
        return (
            f"⛔ 15 分钟内已自我重启 {len(recent)} 次,再试大概率在死循环。已拒绝 —— "
            "请把现状和卡点如实告诉用户,等他拍板再动。"
        )
    if not allow_dirty and git_dirty():
        return (
            "⛔ 工作区有未提交的代码改动。先 git add + git commit(这个 commit 就是"
            "回滚锚点),再调用本工具;确要带脏工作区重启,传 allow_dirty=true(回滚将不可靠)。"
        )
    err = preflight()
    if err:
        return f"⛔ 预检失败,已取消重启(进程还活着,请原地修复后重试):\n{err}"

    head = git_head()
    RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(
        json.dumps(
            {
                "platform": platform,
                "chat_id": chat_id,
                "session_key": session_key,
                "reason": reason,
                "verify_plan": verify_plan,
                "rollback_commit": head,
                "requested_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _record_restart(recent)
    _restart_pending = True
    return (
        "✅ 预检通过,已安排重启:本轮回复结束后进程自动退出,守护脚本约 5 秒后拉起新代码,"
        "然后自动回到本对话执行你的验证计划。"
        f"回滚锚点:{head or '(拿不到 git HEAD)'}。"
        "现在请在正文里简要告诉用户:改了什么、马上重启验证。"
    )


def restart_pending() -> bool:
    return _restart_pending


async def exit_for_restart(adapter: object, chat_id: object) -> None:
    """通知 → 缓冲送达 → 退出。拉起交给 run.sh,自己只负责干净地死。"""
    try:
        await adapter.send(chat_id, "♻️ 正在重启进程加载新代码…约 10 秒后我会回到这条对话继续验证。")
    except Exception:
        pass
    await anyio.sleep(1.5)  # 让 SSE/TG 把上面这条送出去
    os._exit(_EXIT_CODE)


# ── 遗书:读(网关启动侧)──
def consume_resume() -> dict | None:
    """读遗书并【立即删除】—— 无论后续成败绝不二次触发,防重启死循环。"""
    try:
        raw = RESUME_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        RESUME_PATH.unlink()
    except OSError:
        return None  # 删不掉宁可不还魂,也不能冒重复触发的险
    try:
        task = json.loads(raw)
        return task if isinstance(task, dict) else None
    except json.JSONDecodeError:
        return None


def consume_rollback_flag() -> bool:
    if ROLLBACK_FLAG_PATH.exists():
        try:
            ROLLBACK_FLAG_PATH.unlink()
        except OSError:
            pass
        return True
    return False


def build_resume_prompt(task: dict, rolled_back: bool) -> str:
    lines = [
        "[系统消息:自我重启完成]",
        f"你刚才为了「{task.get('reason', '')}」修改了自己的代码并重启,",
    ]
    if rolled_back:
        lines += [
            f"⚠️ 但新代码启动连续失败,守护脚本已回滚到 {task.get('rollback_commit')},当前跑的是旧代码。",
            "请:1)告诉用户改动失败、已自动回滚;2)看 data/logs/hermes.out.log 尾部分析启动失败原因;",
            "3)不要立即再次重启 —— 先把原因和修复思路给用户,等他确认。",
        ]
    else:
        lines += [
            "新代码已加载。现在执行验证计划:",
            task.get("verify_plan", "(遗书里没写验证计划,自行判断要验证什么)"),
            f"验证通过 → 把结果告诉用户;验证失败 → 分析原因,必要时可回滚:"
            f"git reset --hard {task.get('rollback_commit')}(回滚后需再次重启才生效)。",
        ]
    return "\n".join(lines)
