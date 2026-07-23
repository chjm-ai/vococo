"""每会话独立 git worktree —— 借鉴 Claude Code。

病根:旧版同一项目下所有会话共用一个仓库目录,谁 `checkout -b` 都会把整个目录
(以及所有会话看到的分支)一起拽走,于是「分支名全一样」「互相抢分支」。

解法:给每个项目会话开一个物理独立的 git worktree(独立目录 + 独立分支)。同一仓库
可挂 N 个 worktree,各干各的,git 层面根本碰不到对方。worktree 放在 Hermes 自己的
data 目录下(不污染用户项目,免改 .gitignore),路径 data/worktrees/<项目哈希>/<会话slug>。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys

from .. import config
from ..memory import session_store

_WT_BASE = config.DATA_DIR / "worktrees"


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """在 cwd 跑一条 git,返回 (returncode, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
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
    """删 root 里悬空的 hermes/* 空分支:没被任何 worktree 检出、且没超前主分支。返回删除数。

    覆盖两类残留:①手动「新建分支」按钮建了但没 worktree 的(如 hermes/MMDD-HHmm);
    ②删会话时漏删的空分支。有独立提交的分支一律保留,绝不误删工作成果。
    """
    _, out, _ = await _git(root, "worktree", "list", "--porcelain")
    checked = {
        line[len("branch "):].removeprefix("refs/heads/")
        for line in out.splitlines() if line.startswith("branch ")
    }
    _, bout, _ = await _git(
        root, "for-each-ref", "--format=%(refname:short)", "refs/heads/hermes/"
    )
    n = 0
    for br in bout.splitlines():
        br = br.strip()
        if br and br not in checked and await _delete_branch_if_empty(root, br):
            n += 1
    return n


async def ensure_worktree(session_key: str) -> str | None:
    """确保项目会话有独立 worktree;返回其路径,否则 None(回退项目根)。

    幂等:已绑定且目录仍在 → 直接返回。非项目会话 / 非 git 仓库 / 建失败 → None,
    不阻塞对话(这一轮就回落项目根,下一轮再试)。
    """
    root = config.project_root_for(session_key)
    if not root:  # 非项目会话(CLI/TG/main),秒退,零 DB 开销
        return None
    # 项目哈希取自 key 的第二段 p<hash>(project_root_for 已保证 key 是三段项目会话)
    phash = session_key.split(":")[1][1:]
    slug = _slug(session_key.split(":")[-1])
    return await _ensure_worktree_impl(session_key, root, phash, slug)


async def ensure_worktree_for_task(root: str, task_id: str) -> str | None:
    """语音后台任务(voice_dispatch_task 派发)专用:给这次任务开独立 worktree + 分支,
    跟 Web/CLI「一会话一 worktree」是同一套机制,只是绑定 key 换成任务自己的
    `voice-task:<id>`(该 key 本就是这个任务在 session_store 里的落库 key,一对一,
    不会跟别的任务/对话抢)。root 不是 git 仓库(或干脆是 None)就直接 None,任务照常
    在原 cwd 跑,不因为隔离失败而阻塞——语音场景要的是"改代码有分支兜底",不是
    "非项目目录也必须建 worktree"。
    """
    if not root:
        return None
    from ..voice import tasks as voice_tasks  # 懒加载,避免非语音场景也引入 voice 包

    session_key = voice_tasks.session_key(task_id)
    phash = session_store.project_hash(root)
    slug = _slug(task_id)
    return await _ensure_worktree_impl(session_key, root, phash, slug)


async def _ensure_worktree_impl(session_key: str, root: str, phash: str, slug: str) -> str | None:
    existing = session_store.get_worktree(session_key)
    if existing:
        if os.path.isdir(existing):
            return existing
        session_store.clear_worktree(session_key)  # 目录被手删 → 清绑定重来

    if not os.path.isdir(root) or not await _is_git_repo(root):
        return None

    base_dir = _WT_BASE / phash
    os.makedirs(base_dir, exist_ok=True)

    # 主名失败(分支/目录被别处占用等)则换一次带后缀的别名再试,进一步降低回退概率
    last_err = ""
    for suffix in ("", "-2"):
        branch = f"hermes/{slug}{suffix}"
        wt_dir = str(base_dir / f"{slug}{suffix}")
        ok, last_err = await _try_add(root, wt_dir, branch)
        if ok:
            session_store.set_worktree(session_key, wt_dir)
            return wt_dir

    # 全都失败 —— 绝不静默回退 main:响亮报警到日志(含当前分支,方便你立刻发现处理)。
    # 仍返回 None(本轮回退项目根,不炸对话),但这回你在 hermes.out.log 里看得见。
    cur = await _current_branch(root)
    print(
        f"[worktree] ⚠️ 会话 {session_key} 建 worktree 失败,本轮将回退到项目根 "
        f"{root}(当前分支 {cur});若该分支是 main 则本轮改动会落到主分支! "
        f"末次错误: {last_err.strip()[:200]}",
        file=sys.stderr,
        flush=True,
    )
    return None


async def remove_worktree(session_key: str) -> None:
    """会话删除时把它的 worktree 清干净:删目录 + 删空分支 + prune 失效登记。

    删目录后,若它绑的 hermes/* 分支没干活(没超前主分支)就一并删掉,不攒悬空分支;
    有独立提交的分支保留,避免误删工作成果。
    """
    path = session_store.get_worktree(session_key)
    if not path:
        return
    root = config.project_root_for(session_key)
    session_store.clear_worktree(session_key)
    if not (root and os.path.isdir(root)):
        return
    branch = await _branch_of_worktree(root, path)  # 删前先拿到分支名
    await _git(root, "worktree", "remove", "--force", path)
    await _git(root, "worktree", "prune")
    if branch.startswith("hermes/"):
        await _delete_branch_if_empty(root, branch)


async def prune_orphans() -> int:
    """启动兜底:回收「真孤儿」—— data/worktrees 下 DB 已无会话绑定的 worktree,
    以及悬空的 hermes/* 空分支。返回清理总数。

    只碰 hermes 自己那套(data/worktrees + hermes/* 分支),绝不动 Claude Code 的
    .claude/worktrees。活会话(DB 仍绑定)一律跳过。
    """
    if not _WT_BASE.exists():
        return 0
    bound = {os.path.realpath(p) for p in session_store.all_worktree_paths()}
    cleaned = 0
    for phash_dir in _WT_BASE.iterdir():
        if not phash_dir.is_dir():
            continue
        root = session_store.path_for_hash(phash_dir.name)
        if not root or not os.path.isdir(root) or not await _is_git_repo(root):
            continue
        for wt in phash_dir.iterdir():
            if not wt.is_dir() or os.path.realpath(str(wt)) in bound:
                continue  # 活会话绑着 → 跳过
            branch = await _branch_of_worktree(root, str(wt))
            await _git(root, "worktree", "remove", "--force", str(wt))
            if branch.startswith("hermes/"):
                await _delete_branch_if_empty(root, branch)
            cleaned += 1
        await _git(root, "worktree", "prune")
        cleaned += await _prune_dangling_branches(root)  # 顺手清该 repo 的悬空空分支
    return cleaned
