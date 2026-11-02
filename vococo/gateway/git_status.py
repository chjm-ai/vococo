"""项目 git 状态:跑 git 子进程 + 解析 porcelain 输出,供 Web 侧边栏的项目卡片用。

2026-07-23 从 gateway/adapters/web.py 的 WebAdapter 拆出——git porcelain 解析跟
"把请求翻译成 HTTP 响应"是两件事,原先当作 WebAdapter 的私有方法,现在独立成
不依赖 adapter 状态的纯函数,web.py 只负责调用 + 包装成 JSON 响应。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path


async def run_git(cwd: str, *args: str) -> tuple[int, str, str]:
    """在 cwd 里跑一条 git 命令,返回 (returncode, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, "", "Git 状态读取超时"
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
    code, raw, _ = await run_git(cwd, "status", "--porcelain=v1", "--branch", "--untracked-files=all")
    if code != 0:
        return {"is_repo": True, "error": "读取失败"}
    branch, ahead, behind = "", 0, 0
    unborn = False
    files: list[dict] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            # 形如 "main...origin/main [ahead 1, behind 2]" / "HEAD (no branch)"
            # / "No commits yet on main"
            head = line[3:]
            if head.startswith("No commits yet on "):
                unborn = True
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
        if code != 1:
            return {"is_repo": True, "error": "读取失败"}
    if default_branch:
        code, out, _ = await run_git(cwd, "rev-list", "--count", f"{default_branch}..HEAD")
        if code != 0 or not out.strip().isdigit():
            return {"is_repo": True, "error": "读取失败"}
        unmerged = int(out.strip())
    added, removed = 0, 0
    base = "HEAD"
    if unborn:
        code, base, _ = await run_git(cwd, "hash-object", "-t", "tree", "/dev/null")
        if code != 0:
            return {"is_repo": True, "error": "读取失败"}
        base = base.strip()
    code, stat_out, _ = await run_git(cwd, "diff", base, "--shortstat")
    if code != 0:
        return {"is_repo": True, "error": "读取失败"}
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


def _session_repositories(session_key: str, agent: dict) -> list[dict]:
    from .. import config
    from ..core.business_repositories import copies_for_session, repository_sources
    from ..memory import session_store

    repositories = []
    if agent.get("execution_mode") == "project":
        source = config.project_root_for(session_key) or agent["workdir"]
        repositories.append({"project_path": str(Path(source).resolve()),
                             "path": session_store.get_worktree(session_key)})
    for source in repository_sources(agent):
        try:
            binding, = copies_for_session({**agent, "repositories": [source], "links": []}, session_key)
            if source not in agent.get("repositories", []) and not binding.checkout.exists():
                continue  # 尚未执行的关联仓库继续展示原目录状态，不隐式创建副本。
            row = {"project_path": str(binding.source), "path": str(binding.checkout)}
        except (OSError, ValueError):
            row = {"project_path": source, "path": None, "state": "error"}
        if not any(item["project_path"] == row["project_path"] for item in repositories):
            repositories.append(row)
    return repositories


async def _linked_repository(link: dict) -> dict | None:
    """只检查显式关联目录，不递归搜索，也不把普通资料目录当仓库。"""
    path = Path(link["path"]).expanduser().resolve()
    code, root, _ = await run_git(str(path), "rev-parse", "--show-toplevel")
    if code != 0:
        if not (path / ".git").exists():
            return None
        return {"project_path": str(path), "path": str(path), "scope": "shared", "state": "error"}
    root = str(Path(root.strip()).resolve())
    return {"project_path": root, "path": root, "scope": "shared"}


async def _linked_repositories(agent: dict, repositories: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(4)

    async def discover(link):
        async with semaphore:
            try:
                return await _linked_repository(link)
            except (OSError, ValueError):
                return {"project_path": link["path"], "path": link["path"],
                        "scope": "shared", "state": "error"}

    linked = await asyncio.gather(*(discover(link) for link in agent.get("links", [])))
    sources = {row["project_path"] for row in repositories}
    result = []
    for row in linked:
        # 已绑定副本时保持原来的会话隔离口径，同源关联和目录别名不重复计数。
        if row is not None and row["project_path"] not in sources:
            sources.add(row["project_path"])
            result.append(row)
    return result


async def _repository_status(repository: dict) -> dict:
    source, cwd = repository["project_path"], repository["path"]
    row = {"scope": "session", **repository, "name": Path(source).name}
    if row.get("state") == "error":
        return row
    # 展示状态不能隐式初始化副本，更不能回退去读取业务主检出。
    if not cwd or not Path(cwd).exists():
        return {**row, "state": "uninitialized"}
    info = await git_status(cwd)
    state = "error" if info.get("error") or not info.get("is_repo") else "ready"
    return {**row, **info, "state": state}


def _aggregate_repositories(repositories: list[dict], name: str) -> dict:
    ready = [row for row in repositories if row["state"] == "ready"]
    changed = sum(bool(row["dirty"] or row["unmerged"]) for row in ready)
    totals = {field: sum(row[field] for row in ready)
              for field in ("dirty", "added", "removed", "unmerged")}
    return {"is_project": True, "name": name, "repositories": repositories,
            "repo_count": len(repositories), "changed_repos": changed,
            "unavailable": len(repositories) - len(ready), **totals}


async def session_repository_status(session_key: str) -> dict | None:
    """汇总会话副本与显式关联的共享仓库；旧的普通会话由调用方兼容处理。"""
    from ..memory import agents

    agent = agents.agent_for_session(session_key)
    if not agent or agent["id"] == agents.GENERAL_ID:
        return None
    repositories = await asyncio.to_thread(_session_repositories, session_key, agent)
    repositories.extend(await _linked_repositories(agent, repositories))
    if not repositories:
        return {"is_project": False}
    # 限制同时运行的 Git 进程数，多个仓库故障时也不会无限阻塞标题栏。
    semaphore = asyncio.Semaphore(4)

    async def collect(repository):
        async with semaphore:
            try:
                return await _repository_status(repository)
            except (OSError, ValueError):
                return {**repository, "name": Path(repository["project_path"]).name, "state": "error"}

    rows = await asyncio.gather(*(collect(repository) for repository in repositories))
    return _aggregate_repositories(rows, agent["name"])
