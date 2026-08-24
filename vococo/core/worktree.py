"""每会话独立 git worktree —— 借鉴 Claude Code。

病根:旧版同一项目下所有会话共用一个仓库目录,谁 `checkout -b` 都会把整个目录
(以及所有会话看到的分支)一起拽走,于是「分支名全一样」「互相抢分支」。

解法:给每个项目会话开一个物理独立的 git worktree(独立目录 + 独立分支)。同一仓库
可挂 N 个 worktree,各干各的,git 层面根本碰不到对方。worktree 放在各会话项目自己
的 data 目录下(data/ 已在各项目 .gitignore 里,不污染源码树),路径
<项目>/data/worktrees/<项目哈希>/<会话slug>。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import shutil
import sys
from pathlib import Path

from .. import config
from ..memory import session_store

_GIT_TIMEOUT_SEC = 8


def _wt_base(root: str) -> Path:
    """worktree 统一放「会话项目自己的 data/worktrees」下:
    vococo 项目 = vococo/data/worktrees(位置不变),其他项目各归各的 data/。
    data/ 已在各项目 .gitignore 里,不污染源码树。"""
    return Path(root) / "data" / "worktrees"


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """在 cwd 跑一条 git,返回 (returncode, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SEC)
    except TimeoutError:
        # iCloud/网络盘上的仓库偶尔会让 git 永久卡住；清理只是尽力而为，不能拖死 serve。
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        return 124, "", f"git 命令超时({_GIT_TIMEOUT_SEC}s)"
    except (OSError, ValueError) as e:
        return 127, "", str(e)
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _is_git_repo(path: str) -> bool:
    code, out, _ = await _git(path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


async def _current_branch(root: str) -> str:
    """root 当前检出的分支名;游离/异常返回标记。"""
    code, out, _ = await _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if code == 0 else "(detached)"


def _slug(conv: str) -> str:
    """conv id → 安全的分支/目录名片段(只留字母数字和 .-_)。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "-", conv).strip("-._")
    return s or "session"


async def _try_add(root: str, wt_dir: str, branch: str) -> tuple[bool, str]:
    """在 wt_dir 建 worktree 绑到 branch;分支已存在则复用,否则新建。返回 (是否成功, 错误)。

    add 前先自愈两类最常见的失败源:①失效的 worktree 登记(目录被手删但 git 还记着)
    → git worktree prune;②同名残留目录(上次 add 半途失败/没清干净)→ 先正规 remove,
    仍在则强删。清干净再 add,把「建失败静默回退 main」的触发概率压到极低。
    """
    await _git(root, "worktree", "prune")
    if os.path.exists(wt_dir):
        await _git(root, "worktree", "remove", "--force", wt_dir)
        if os.path.exists(wt_dir):
            shutil.rmtree(wt_dir, ignore_errors=True)
    exists, _, _ = await _git(
        root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    )
    add_args = ("worktree", "add", wt_dir, branch) if exists == 0 else (
        "worktree", "add", wt_dir, "-b", branch
    )
    code, _, err = await _git(root, *add_args)
    return code == 0, err


async def _default_branch(root: str) -> str:
    """root 的默认主分支名(main 优先,其次 master),用于判断某分支是否「没干活」。"""
    for b in ("main", "master"):
        code, _, _ = await _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")
        if code == 0:
            return b
    return "main"


async def _branch_of_worktree(root: str, path: str) -> str:
    """查 path 这个 worktree 当前绑定的分支名(删 worktree 前用,删完就查不到了)。"""
    _, out, _ = await _git(root, "worktree", "list", "--porcelain")
    cur = None
    rp = os.path.realpath(path)
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and cur and os.path.realpath(cur) == rp:
            return line[len("branch "):].removeprefix("refs/heads/")
    return ""


async def _delete_branch_if_empty(root: str, branch: str) -> bool:
    """分支没超前默认主分支(=空分支、没干活)就删掉,返回是否删了;有独立提交则保留。"""
    base = await _default_branch(root)
    code, out, _ = await _git(root, "rev-list", "--count", f"{base}..{branch}")
    if code == 0 and out.strip() == "0":
        await _git(root, "branch", "-D", branch)  # 空分支,安全删,不攒悬空
        return True
    return False


async def _prune_dangling_branches(root: str) -> int:
    """删 root 里悬空的 vococo/* 空分支:没被任何 worktree 检出、且没超前主分支。返回删除数。

    覆盖两类残留:①手动「新建分支」按钮建了但没 worktree 的(如 vococo/MMDD-HHmm);
    ②删会话时漏删的空分支。有独立提交的分支一律保留,绝不误删工作成果。
    同时扫 hermes/*(2026-08 改名前的存量前缀),一并清理。
    """
    _, out, _ = await _git(root, "worktree", "list", "--porcelain")
    checked = {
        line[len("branch "):].removeprefix("refs/heads/")
        for line in out.splitlines() if line.startswith("branch ")
    }
    bout = ""
    for prefix in ("refs/heads/vococo/", "refs/heads/hermes/"):
        _, o, _ = await _git(root, "for-each-ref", "--format=%(refname:short)", prefix)
        bout += o
    n = 0
    for br in bout.splitlines():
        br = br.strip()
        if br and br not in checked and await _delete_branch_if_empty(root, br):
            n += 1
    return n


class WorktreeIsolationError(RuntimeError):
    """Git 项目会话无法拿到独立 worktree 时中止执行,禁止落回主检出目录。"""


async def ensure_worktree(session_key: str) -> str | None:
    """确保一次会话有独立 worktree;非 Git 目录返回 None。"""
    root = config.execution_project_root_for(session_key)
    if not root:
        return None
    # 项目哈希按真实根目录计算:默认项目没有 web:p<hash> 会话 key。
    phash = session_store.project_hash(root)
    slug = _slug(session_key.split(":")[-1])
    return await _ensure_worktree_impl(session_key, root, phash, slug)


async def ensure_worktree_for_task(root: str, task_id: str) -> str | None:
    """给后台任务开独立 worktree;非 Git 目录返回 None。"""
    if not root:
        return None
    from . import tasks as bg_tasks  # 懒加载,避免非任务场景也引入这块

    session_key = bg_tasks.session_key(task_id)
    phash = session_store.project_hash(root)
    slug = _slug(task_id)
    return await _ensure_worktree_impl(session_key, root, phash, slug)


async def _execution_cwd(root: str, session_key: str, candidate: str | None) -> str:
    """收敛所有入口的隔离策略:Git 项目没有 worktree 就拒绝执行。"""
    if candidate:
        return candidate
    if root and await _is_git_repo(root):
        raise WorktreeIsolationError(
            f"Git 项目会话未能创建独立工作树，已停止执行以保护主分支: {root}"
        )
    return root


async def execution_cwd(session_key: str) -> str:
    """普通会话的实际 cwd;Git 项目必须成功绑定 worktree。"""
    root = config.execution_project_root_for(session_key)
    wt = await ensure_worktree(session_key)
    return await _execution_cwd(root, session_key, wt)


async def execution_cwd_for_task(root: str, task_id: str) -> str:
    """后台任务的实际 cwd;Git 项目必须成功绑定 worktree。"""
    wt = await ensure_worktree_for_task(root, task_id)
    from . import tasks as bg_tasks  # 懒加载,避免非任务场景也引入这块

    return await _execution_cwd(root, bg_tasks.session_key(task_id), wt)


async def _ensure_worktree_impl(session_key: str, root: str, phash: str, slug: str) -> str | None:
    existing = session_store.get_worktree(session_key)
    if existing:
        if os.path.isdir(existing):
            return existing
        session_store.clear_worktree(session_key)  # 目录被手删 → 清绑定重来

    if not os.path.isdir(root) or not await _is_git_repo(root):
        return None

    base_dir = _wt_base(root) / phash
    os.makedirs(base_dir, exist_ok=True)

    # 主名失败(分支/目录被别处占用等)则换一次带后缀的别名再试,进一步降低回退概率
    last_err = ""
    for suffix in ("", "-2"):
        branch = f"vococo/{slug}{suffix}"
        wt_dir = str(base_dir / f"{slug}{suffix}")
        ok, last_err = await _try_add(root, wt_dir, branch)
        if ok:
            session_store.set_worktree(session_key, wt_dir)
            return wt_dir

    # 全都失败:调用方会拒绝在 Git 项目继续执行,绝不回退到主检出目录。
    cur = await _current_branch(root)
    print(
        f"[worktree] ⚠️ 会话 {session_key} 建 worktree 失败,已阻止在项目根执行 "
        f"{root}(当前分支 {cur});末次错误: {last_err.strip()[:200]}",
        file=sys.stderr,
        flush=True,
    )
    return None


async def recycle_empty_worktree(session_key: str) -> bool:
    """归档等「会话收尾」场景:worktree 是空壳就立即回收,有内容则不动。返回是否回收。

    空壳判定 = 分支没超前主分支(从未提交过) 且 工作区干净(含未跟踪文件——新代码
    没 add 前就是未跟踪状态,不能当临时产物无声丢掉)。与 worktree_dirty_summary
    同一语义:归档弹窗说有内容,回收就必须真保留,否则提示「未回收」实际却删了。
    满足说明这个会话从没改过代码,worktree/分支都没有保留价值;有独立提交或有
    未提交改动的留着,交给合并(merge-main.sh)或删会话(remove_worktree)流程,
    绝不自动丢内容。
    """
    path = session_store.get_worktree(session_key)
    if not path or not os.path.isdir(path):
        return False
    root = config.execution_project_root_for(session_key)
    if not (root and os.path.isdir(root)):
        return False
    branch = await _branch_of_worktree(root, path)
    if not branch:
        return False
    # ① 分支没超前主分支(没干过活)
    base = await _default_branch(root)
    code, out, _ = await _git(root, "rev-list", "--count", f"{base}..{branch}")
    if not (code == 0 and out.strip() == "0"):
        return False
    # ② 工作区干净(完整 status:未跟踪文件也算内容,见函数文档)
    _, st, _ = await _git(path, "status", "--porcelain")
    if st.strip():
        return False
    # 空壳 → 回收
    session_store.clear_worktree(session_key)
    await _git(root, "worktree", "remove", "--force", path)
    await _git(root, "worktree", "prune")
    await _git(root, "branch", "-D", branch)
    return True


async def worktree_dirty_summary(session_key: str) -> dict | None:
    """会话 worktree 的未提交/未合并内容摘要;干净或没有 worktree 返回 None。

    归档/删除前调用:有未提交改动(含未跟踪)或分支有独立提交时,删 worktree 会
    丢这些内容,必须先告知用户。返回 {"uncommitted": n, "untracked": n, "commits": n}。
    """
    path = session_store.get_worktree(session_key)
    if not path or not os.path.isdir(path):
        return None
    root = config.execution_project_root_for(session_key)
    if not (root and os.path.isdir(root)):
        return None
    _, st, _ = await _git(path, "status", "--porcelain")
    # status 里 ?? 开头的是未跟踪,其余是已跟踪的改动
    uncommitted = sum(1 for l in st.splitlines() if l.strip() and not l.startswith("??"))
    untracked = sum(1 for l in st.splitlines() if l.startswith("??"))
    branch = await _branch_of_worktree(root, path)
    commits = 0
    if branch:
        base = await _default_branch(root)
        code, out, _ = await _git(root, "rev-list", "--count", f"{base}..{branch}")
        commits = int(out.strip()) if code == 0 and out.strip() else 0
    if uncommitted == 0 and untracked == 0 and commits == 0:
        return None
    return {"uncommitted": uncommitted, "untracked": untracked, "commits": commits}


async def remove_worktree(session_key: str) -> None:
    """会话删除时把它的 worktree 清干净:删目录 + 删空分支 + prune 失效登记。

    删目录后,若它绑的 vococo/* 分支没干活(没超前主分支)就一并删掉,不攒悬空分支;
    有独立提交的分支保留,避免误删工作成果。
    """
    path = session_store.get_worktree(session_key)
    if not path:
        return
    root = config.execution_project_root_for(session_key)
    session_store.clear_worktree(session_key)
    if not (root and os.path.isdir(root)):
        return
    branch = await _branch_of_worktree(root, path)  # 删前先拿到分支名
    await _git(root, "worktree", "remove", "--force", path)
    await _git(root, "worktree", "prune")
    if branch.startswith(("vococo/", "hermes/")):  # hermes/ 是改名前存量,同样清
        await _delete_branch_if_empty(root, branch)


async def prune_orphans() -> int:
    """启动兜底:回收「真孤儿」—— 各项目 data/worktrees 下 DB 已无会话绑定的
    worktree,以及悬空的 vococo/* 空分支。返回清理总数。

    回收依据只有用户意图:归档(archived→recycle_empty_worktree)和删除会话
    (remove_worktree)。系统不做推断回收——DB 还绑着的 worktree 一律不动,哪怕
    会话早不活跃、代码干净,用户可能随时回来继续聊。

    遍历全部项目(vococo 自己的 data/worktrees 位置不变 + 其他项目各归各的),
    绝不动 Claude Code 的 .claude/worktrees。分支只认 vococo/* 及改名前存量
    hermes/*。
    """
    bound = {os.path.realpath(p) for p in session_store.all_worktree_paths()}
    cleaned = 0
    roots: dict[str, str] = {
        os.path.realpath(proj["path"]): proj["path"]
        for proj in session_store.list_projects()
    }
    # 默认项目不在 projects 表里,但它的会话同样会创建 worktree,需要纳入启动清理。
    default_root = str(config.ROOT_DIR)
    roots.setdefault(os.path.realpath(default_root), default_root)
    for root in roots.values():
        if not os.path.isdir(root) or not await _is_git_repo(root):
            continue
        base = _wt_base(root) / session_store.project_hash(root)
        if not base.exists():
            continue
        for wt in base.iterdir():
            if not wt.is_dir() or os.path.realpath(str(wt)) in bound:
                continue  # 有主(DB 绑定)的 worktree → 一律跳过
            branch = await _branch_of_worktree(root, str(wt))
            await _git(root, "worktree", "remove", "--force", str(wt))
            if branch.startswith(("vococo/", "hermes/")):
                await _delete_branch_if_empty(root, branch)
            cleaned += 1
        await _git(root, "worktree", "prune")
        cleaned += await _prune_dangling_branches(root)  # 顺手清该 repo 的悬空空分支
    return cleaned
