"""项目 git 状态:跑 git 子进程 + 解析 porcelain 输出,供 Web 侧边栏的项目卡片用。

2026-07-23 从 gateway/adapters/web.py 的 WebAdapter 拆出——git porcelain 解析跟
"把请求翻译成 HTTP 响应"是两件事,原先当作 WebAdapter 的私有方法,现在独立成
不依赖 adapter 状态的纯函数,web.py 只负责调用 + 包装成 JSON 响应。
"""
from __future__ import annotations

import asyncio
import re


async def run_git(cwd: str, *args: str) -> tuple[int, str, str]:
    """在 cwd 里跑一条 git 命令,返回 (returncode, stdout, stderr)。"""
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


async def git_status(cwd: str) -> dict:
    """收集一份 git 状态:分支、领先/落后、改动文件清单。"""
    code, out, _ = await run_git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return {"is_repo": False}
    _, raw, _ = await run_git(cwd, "status", "--porcelain=v1", "--branch")
    branch, ahead, behind = "", 0, 0
    files: list[dict] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            # 形如 "main...origin/main [ahead 1, behind 2]" / "HEAD (no branch)"
            # / "No commits yet on main"
            head = line[3:]
            if head.startswith("No commits yet on "):
                branch = head[len("No commits yet on "):].strip()
                continue
            branch = head.split(" ", 1)[0].split("...", 1)[0]
            if m := re.search(r"ahead (\d+)", head):
                ahead = int(m.group(1))
            if m := re.search(r"behind (\d+)", head):
                behind = int(m.group(1))
        elif line:
            files.append({"x": line[:2], "path": line[3:]})  # XY 状态码 + 路径
    # 未合并提交:相对默认分支(main/master)的领先提交数——「这个分支干的活还没回到主
    # 分支」。注意与上面的 ahead 是两回事:status --branch 的 ahead 相对【远程上游】,
    # 本地开发分支(vococo/xxx)常不 push 远程、没有上游,ahead 恒为 0,标题栏只看 ahead
    # 会以为没改动;这里用 rev-list 数 main..HEAD,与 worktree_dirty_summary 同一语义。
    unmerged = 0
    default_branch = ""
    for b in ("main", "master"):
        code, _, _ = await run_git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")
        if code == 0:
            default_branch = b
            break
    if default_branch:
        code, out, _ = await run_git(cwd, "rev-list", "--count", f"{default_branch}..HEAD")
        if code == 0 and out.strip().isdigit():
            unmerged = int(out.strip())
    added, removed = 0, 0
    _, stat_out, _ = await run_git(cwd, "diff", "HEAD", "--shortstat")
    if stat_out.strip():
        if m := re.search(r"(\d+) insertion", stat_out):
            added = int(m.group(1))
        if m := re.search(r"(\d+) deletion", stat_out):
            removed = int(m.group(1))
    return {
        "is_repo": True,
        "branch": branch or "(游离 HEAD)",
        "ahead": ahead,
        "behind": behind,
        "unmerged": unmerged,
        "dirty": len(files),
        "files": files[:60],  # 改动太多只回前 60 条,够看
        "added": added,
        "removed": removed,
    }
