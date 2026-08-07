"""自我运维 —— agent 改完自身代码后的「重启 + 还魂验证」闭环。

流程(遗书 + 还魂):
1. agent 调 restart_self 工具(定义在 builtin.py,逻辑在这)→ 预检新代码
   (compileall 全量语法 + 主链 import)→ 写下 data/resume_task.json
   (遗书:对话在哪、验证什么)和独立的 restart transaction
   (稳定版 + 候选版)→ 标记待重启。
2. 网关在【本轮回复完整结束、历史落库之后】看到标记 → 通知用户 → 进程退出。
   拉起完全交给 deploy/run.sh 守护循环 —— 自己绝不 spawn 新进程,
   保证任何时刻只有一个实例(否则孤儿进程会抢 TG 轮询)。
3. 新进程启动 → GatewayRunner 读到遗书(读完即删,防重复触发)→ 往原对话
   注入一条系统消息,agent 带着 SQLite 里的完整历史继续执行验证计划。

保险丝:
- 预检不过 → 不重启,原地报错(防「重启进坟墓」:语法错则新进程根本起不来)。
- 15 分钟内最多 3 次自我重启,超了拒绝(防「改→挂→再改」无人值守死循环)。
- 工作区有未提交改动默认拒绝(commit 即回滚锚点,锚点必须干净才可靠)。
- 新代码启动即崩 → run.sh 连崩 3 次后按独立事务里的稳定版本回滚,
  并 touch data/.rollback_done;还魂消息据此改为「已回滚,别再重启」。
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio

from .. import config

_REPO_ROOT = config.DATA_DIR.parent
RESUME_PATH = config.DATA_DIR / "resume_task.json"
RESTART_STAMPS_PATH = config.DATA_DIR / "self_restarts.json"
ROLLBACK_FLAG_PATH = config.DATA_DIR / ".rollback_done"
RESTART_TRANSACTION_PATH = config.DATA_DIR / "restart_transaction.json"
RESTART_TRANSACTION_LOCK_PATH = config.DATA_DIR / ".restart_transaction.lock"
RESTART_FAILURE_PATH = config.DATA_DIR / "restart_failure.json"
RUNNING_REVISION_PATH = config.DATA_DIR / "running_revision.json"
STABLE_REVISION_PATH = config.DATA_DIR / "stable_revision.json"
SUPERVISOR_PID_PATH = config.DATA_DIR / "supervisor.pid"

_RATE_WINDOW_SEC = 15 * 60
_RATE_MAX = 3
_EXIT_CODE = 51  # 区别于崩溃的退出码,run.sh 日志里一眼认出"这是主动重启"
_STABLE_WINDOW_SEC = 20

# 每会话的重启标志(按 session_key):一个会话最多一次,同一时刻只有被授权的会话能标记
_restart_pending: dict[str, dict] = {}

# 语音模式的还魂数据桥(由 _resume_after_restart 写入,_handle_send 消费):
# GatewayRunner 启动时读到语音的遗书后,不 dispatch(没有 voice adapter),
# 而是暂存到这里,让下次语音消息到来时由 _handle_send 注入验证计划。
_voice_resume: dict | None = None


def save_voice_resume(task: dict, rolled_back: bool) -> None:
    global _voice_resume
    _voice_resume = {"task": task, "rolled_back": rolled_back}


def take_voice_resume() -> dict | None:
    global _voice_resume
    data = _voice_resume
    _voice_resume = None
    return data


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
    """工作区有未提交改动（包括未跟踪文件）?"""
    try:
        r = _git("status", "--porcelain")
        if r.returncode != 0:
            return True
        return bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


def preflight() -> str | None:
    """新代码健康检查;返回 None=通过,否则错误摘要。

    compileall 抓全量语法错误;import 主链抓 import 期错误(含懒加载的
    web adapter —— 自我修改最常动的就是它)。
    """
    checks = [
        (["-m", "compileall", "-q", "vococo"], "语法检查"),
        (
            [
                "-c",
                "import vococo.gateway.run, "
                "vococo.gateway.adapters.web",
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
def _recent_restarts() -> list[object]:
    try:
        stamps = json.loads(RESTART_STAMPS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        stamps = []
    if not isinstance(stamps, list):
        return []
    now = time.time()
    recent = []
    for stamp in stamps:
        requested_at = (
            stamp
            if isinstance(stamp, (int, float))
            else stamp.get("requested_at") if isinstance(stamp, dict) else None
        )
        if (
            isinstance(requested_at, (int, float))
            and now - requested_at < _RATE_WINDOW_SEC
        ):
            recent.append(stamp)
    return recent


def _record_restart(recent: list[object], restart_token: str) -> None:
    _atomic_write_json(
        RESTART_STAMPS_PATH,
        recent + [{"token": restart_token, "requested_at": time.time()}],
    )


def _remove_restart_stamp(restart_token: str) -> None:
    """只撤销指定事务的限流记录，保留其它会话与旧格式历史。"""
    try:
        stamps = json.loads(RESTART_STAMPS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(stamps, list):
        return
    kept = [
        stamp
        for stamp in stamps
        if not (isinstance(stamp, dict) and stamp.get("token") == restart_token)
    ]
    if len(kept) != len(stamps):
        _atomic_write_json(RESTART_STAMPS_PATH, kept)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, data: object) -> None:
    """同目录临时文件 + replace，避免进程退出时留下半截 JSON。"""
    # 先完整序列化，再创建临时文件；不可序列化的数据不会留下半截临时文件。
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[selfops] 临时 JSON 清理失败 {tmp}: {exc}", flush=True)


def _create_restart_transaction(data: dict) -> bool:
    """完整写好临时文件后原子发布；进程崩溃不会留下半截事务。"""
    RESTART_TRANSACTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    claim = RESTART_TRANSACTION_PATH.with_name(
        f".{RESTART_TRANSACTION_PATH.name}.{os.getpid()}.{time.time_ns()}.claim"
    )
    try:
        with RESTART_TRANSACTION_LOCK_PATH.open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False

            existing = _read_json(RESTART_TRANSACTION_PATH)
            if existing is not None and _valid_restart_transaction(existing):
                return False
            if RESTART_TRANSACTION_PATH.exists():
                RESTART_TRANSACTION_PATH.unlink()

            with claim.open("x", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            try:
                # hard link 是“不覆盖”的原子发布：目标要么不存在，要么已是完整 JSON。
                os.link(claim, RESTART_TRANSACTION_PATH)
            except FileExistsError:
                return False
            return True
    finally:
        try:
            claim.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # target 已发布时，claim 只是无害垃圾，不能把成功事务改判为失败。
            print(f"[selfops] claim 清理失败 {claim}: {exc}", flush=True)


def _valid_restart_transaction(data: dict) -> bool:
    return (
        isinstance(data.get("stable_revision"), str)
        and bool(data["stable_revision"])
        and isinstance(data.get("candidate_revision"), str)
        and bool(data["candidate_revision"])
        and isinstance(data.get("session_key"), str)
        and bool(data["session_key"])
        and isinstance(data.get("requested_at"), (int, float))
        and isinstance(data.get("restart_token"), str)
        and bool(data["restart_token"])
    )


def _supervisor_alive() -> bool:
    """确认 PID 存活且命令确为本仓库的前台监督者，防止 PID 复用。"""
    try:
        pid = int(SUPERVISOR_PID_PATH.read_text(encoding="utf-8").strip())
        if pid <= 1:
            return False
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        tokens = shlex.split(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False

    expected_script = (_REPO_ROOT / "deploy" / "run.sh").resolve()
    for index, token in enumerate(tokens[:-1]):
        if token == "--foreground":
            continue
        try:
            script = Path(token).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if script == expected_script and tokens[index + 1] == "--foreground":
            return True
    return False


def _discard_restart_state(session_key: str, reason: str) -> None:
    """监督者失联时撤销本会话事务并留下可诊断记录。"""
    _restart_pending.pop(session_key, None)
    transaction = _read_json(RESTART_TRANSACTION_PATH)
    restart_token = (
        transaction.get("restart_token")
        if transaction and transaction.get("session_key") == session_key
        else None
    )
    # 事务文件仍在时其它请求会被 single-flight 拒绝；先撤 stamp 再删事务，
    # 避免新请求并发读写 stamps 时把已撤销的 token 带回来。
    if isinstance(restart_token, str) and restart_token:
        try:
            _remove_restart_stamp(restart_token)
        except OSError as exc:
            print(f"[selfops] 撤销重启限流记录失败: {exc}", flush=True)
    for path in (RESUME_PATH, RESTART_TRANSACTION_PATH):
        state = _read_json(path)
        if state is None or state.get("session_key") == session_key:
            try:
                path.unlink()
            except OSError:
                pass
    try:
        _atomic_write_json(
            RESTART_FAILURE_PATH,
            {
                "session_key": session_key,
                "reason": reason,
                "failed_at": int(time.time()),
            },
        )
    except (OSError, TypeError, ValueError):
        print(f"[selfops] 无法写入重启失败记录: {reason}", flush=True)


def supervisor_ready_for_exit(session_key: str) -> bool:
    if _supervisor_alive():
        return True
    _discard_restart_state(session_key, "正式监督者在退出前失联，已取消自我重启")
    return False


def stable_revision() -> str | None:
    data = _read_json(STABLE_REVISION_PATH)
    revision = data.get("revision") if data else None
    return revision if isinstance(revision, str) and revision else None


def write_running_revision(revision: str | None = None) -> str | None:
    """记录本次进程实际运行的版本，供稳定窗口任务核对。"""
    revision = revision or git_head()
    if not revision:
        return None
    _atomic_write_json(
        RUNNING_REVISION_PATH,
        {"revision": revision, "pid": os.getpid(), "started_at": int(time.time())},
    )
    return revision


async def mark_runtime_stable(delay_sec: float = _STABLE_WINDOW_SEC) -> bool:
    """存活满稳定窗口后晋升版本；只清理与本进程相同候选版的事务。"""
    await anyio.sleep(delay_sec)
    running = _read_json(RUNNING_REVISION_PATH)
    if not running or running.get("pid") != os.getpid():
        return False
    revision = running.get("revision")
    current_revision = await anyio.to_thread.run_sync(git_head)
    if (
        not isinstance(revision, str)
        or not revision
        or current_revision != revision
    ):
        return False

    transaction = _read_json(RESTART_TRANSACTION_PATH)
    if transaction is not None and transaction.get("candidate_revision") != revision:
        return False
    _atomic_write_json(
        STABLE_REVISION_PATH,
        {"revision": revision, "pid": os.getpid(), "stable_at": int(time.time())},
    )
    if transaction is not None:
        try:
            RESTART_TRANSACTION_PATH.unlink()
        except OSError:
            return False
    return True


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
    if session_key in _restart_pending:
        return "已有一个重启在排队(本轮回复结束即执行),不用重复调用。"
    if not _supervisor_alive():
        return (
            "⛔ 正式监督者不存在或 PID 无效，当前进程退出后可能无法自动拉起。"
            "已拒绝重启；请先用 deploy/launchd.sh install 恢复监督者。"
        )
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

    candidate = git_head()
    stable = stable_revision()
    if not candidate:
        return "⛔ 无法读取当前 Git 版本，已拒绝创建重启事务。"
    if not stable:
        return "⛔ 尚无经过稳定运行窗口确认的稳定版本，已拒绝自我重启。"

    requested_at = int(time.time())
    restart_token = uuid.uuid4().hex
    transaction = {
        "stable_revision": stable,
        "candidate_revision": candidate,
        "session_key": session_key,
        "requested_at": requested_at,
        "restart_token": restart_token,
    }
    try:
        transaction_created = _create_restart_transaction(transaction)
    except OSError as exc:
        return f"⛔ 创建全局重启事务失败，已取消重启：{exc}"
    if not transaction_created:
        return "⛔ 已有一个全局重启事务在执行，当前请求未覆盖它。"

    task_data = {
        "platform": platform,
        "chat_id": chat_id,
        "session_key": session_key,
        "reason": reason,
        "verify_plan": verify_plan,
        "rollback_commit": stable,
        "candidate_revision": candidate,
        "requested_at": requested_at,
        "restart_token": restart_token,
    }
    try:
        _atomic_write_json(RESUME_PATH, task_data)
        _record_restart(recent, restart_token)
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        for path in (RESUME_PATH, RESTART_TRANSACTION_PATH):
            try:
                path.unlink()
            except OSError:
                pass
        return f"⛔ 写入重启状态失败，已取消重启：{exc}"
    _restart_pending[session_key] = task_data  # 仅标记该会话
    return (
        "✅ 预检通过,已安排重启:本轮回复结束后进程自动退出,守护脚本约 5 秒后拉起新代码,"
        "然后自动回到本对话执行你的验证计划。"
        f"稳定回滚锚点:{stable}；候选版本:{candidate}。"
        "现在请在正文里简要告诉用户:改了什么、马上重启验证。"
    )


def restart_pending(session_key: str) -> bool:
    """检查指定会话是否标记了待重启。"""
    return session_key in _restart_pending


def pop_restart_pending(session_key: str) -> dict | None:
    """消费该会话的待重启标记(消费即删)—— 确保只退出一次。"""
    return _restart_pending.pop(session_key, None)


async def exit_for_restart(
    adapter: object, chat_id: object, session_key: str
) -> bool:
    """通知 → 缓冲送达 → 退出。拉起交给 run.sh,自己只负责干净地死。"""
    if not supervisor_ready_for_exit(session_key):
        try:
            await adapter.send(chat_id, "⛔ 正式监督者已失联，本次重启已取消，当前服务继续运行。")
        except Exception:
            pass
        return False
    try:
        await adapter.send(chat_id, "♻️ 正在重启进程加载新代码…约 10 秒后我会回到这条对话继续验证。")
    except Exception:
        pass
    await anyio.sleep(1.5)  # 让 SSE/TG 把上面这条送出去
    if not supervisor_ready_for_exit(session_key):
        try:
            await adapter.send(chat_id, "⛔ 正式监督者已失联，本次重启已取消，当前服务继续运行。")
        except Exception:
            pass
        return False
    try:
        from ..core import client_pool  # 懒加载,避免 import 环

        await client_pool.close_all()  # 收掉保温的 CLI 子进程再退,不留孤儿
    except Exception:
        pass
    # close_all 期间也可能发生监督者崩溃；紧贴 os._exit 再核对一次。
    if not supervisor_ready_for_exit(session_key):
        try:
            await adapter.send(chat_id, "⛔ 正式监督者已失联，本次重启已取消，当前服务继续运行。")
        except Exception:
            pass
        return False
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


# 入库的可见「用户轮」文本前缀:前端据此把这条渲染成【居中系统条】而非用户气泡。
# 给 agent 的完整指令(build_resume_prompt)只在当轮送入模型,不入库,避免长指令
# 既污染上下文、又在刷新后被当成「用户发的话」显示。
SYS_MARKER = "⚙️[系统]"


def build_resume_store_text(task: dict, rolled_back: bool) -> str:
    """这一轮存进会话库的简短 user 文本(带系统标记,前端渲染成系统条)。"""
    if rolled_back:
        return f"{SYS_MARKER} 自我重启后新代码启动失败,已自动回滚到旧版本。"
    return f"{SYS_MARKER} 自我重启完成,已加载新代码并自动继续验证。"


def build_resume_prompt(task: dict, rolled_back: bool) -> str:
    lines = [
        "[系统消息:自我重启完成]",
        f"你刚才为了「{task.get('reason', '')}」修改了自己的代码并重启,",
    ]
    if rolled_back:
        lines += [
            f"⚠️ 但新代码启动连续失败,守护脚本已回滚到 {task.get('rollback_commit')},当前跑的是旧代码。",
            "请:1)告诉用户改动失败、已自动回滚;2)看 data/logs/vococo.out.log 尾部分析启动失败原因;",
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
