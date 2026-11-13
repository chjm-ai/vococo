"""关联目录自动识别 Git 仓库，每会话使用独立副本避免并发覆盖。"""
from __future__ import annotations

import asyncio
import hashlib
import fcntl
import logging
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .. import config


@dataclass(frozen=True)
class RepositoryCopy:
    source: Path
    checkout: Path
    branch: str


def normalize_repositories(value) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError("代码仓库必须是目录路径列表")
    paths = list(dict.fromkeys(str(Path(x).expanduser().resolve()) for x in value))
    for path in paths:
        try:
            root = _git(Path(path), "rev-parse", "--show-toplevel")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"请选择有效的 Git 仓库根目录：{path}") from exc
        if Path(root).resolve() != Path(path):
            raise ValueError(f"请选择仓库根目录，而不是子目录：{path}")
    return paths


def repository_sources(agent: dict) -> list[str]:
    """只识别关联目录本身，不递归扫描；保留旧仓库配置的兼容读取。"""
    roots = list(agent.get("repositories", []))
    for link in agent.get("links", []):
        path = Path(link["path"]).expanduser().resolve()
        if not path.is_dir():
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue  # 目录授权不依赖 Git 是否可用；状态面板单独报告仓库故障。
        if result.returncode == 0:
            roots.append(str(Path(result.stdout.strip()).resolve()))
    return list(dict.fromkeys(roots))


def session_sources(agent: dict, session_key: str) -> list[str]:
    from ..memory import session_store
    from . import permissions

    roots = repository_sources(agent)
    access = session_store.active_source_access(session_key)
    if access and access.get("mode") != permissions.FULL:
        return roots  # 自动来源不继承交互会话的临时授权。
    full = permissions.is_full(session_key)
    roots.extend(source for source, authorization in
                 session_store.repository_grants(session_key, agent["id"]).items()
                 if authorization == "approved" or full)
    return list(dict.fromkeys(roots))


def copy_for_source(agent_id: str, session_key: str, source: str) -> RepositoryCopy:
    session = hashlib.sha256(session_key.encode()).hexdigest()[:20]
    base = config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories" / agent_id / session
    root = Path(source).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    checkout = base / digest
    if checkout.absolute() != checkout.resolve():
        raise ValueError(f"仓库副本路径不能经过软链接：{checkout}")
    return RepositoryCopy(root, checkout, f"vococo/business-{session}-{digest}")


def copies_for_session(agent: dict, session_key: str) -> tuple[RepositoryCopy, ...]:
    return tuple(copy_for_source(agent["id"], session_key, source)
                 for source in session_sources(agent, session_key))


def _git(cwd: Path, *args: str) -> str:
    timeout = 300 if args[:2] == ("worktree", "add") else 60
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        # 不把可能含远程凭据的 Git stderr 带进提示词或接口回执。
        raise ValueError(f"仓库副本 Git 操作失败（{args[0]}），请检查目录和仓库状态：{cwd}")
    return result.stdout.strip()


def _git_path(checkout: Path, option: str) -> Path:
    return Path(_git(checkout, "rev-parse", "--path-format=absolute", option)).resolve()


def _verify_copy(binding: RepositoryCopy) -> None:
    checkout = binding.checkout
    marker = checkout / ".git"
    if checkout.absolute() != checkout.resolve() or marker.is_symlink() or not marker.exists():
        raise ValueError(f"仓库副本 Git 目录无效：{checkout}")
    if Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError(f"仓库副本根目录不匹配：{checkout}")
    common = _git_path(checkout, "--git-common-dir")
    if marker.is_dir():
        # 旧版本的完整克隆原地复用，不能丢弃已有提交或未提交改动。
        if (common != marker or (common / "objects/info/alternates").exists()
                or _git(checkout, "config", "--get", "vococo.source") != str(binding.source)):
            raise ValueError(f"仓库副本初始化未完成或来源不匹配：{checkout}")
        return
    git_dir = _git_path(checkout, "--git-dir")
    if ((checkout.name.startswith(".initializing-") and (git_dir / "locked").exists())
            or (not (git_dir / "index").exists()
                and _git(checkout, "rev-parse", "--revs-only", "HEAD"))):
        raise ValueError(
            f"仓库副本上次初始化未完成，现场已保留，未解锁或覆盖：{checkout}。"
            "普通聊天无需该副本；需要修改代码时请新建会话重试。"
        )
    if (common != _git_path(binding.source, "--git-common-dir")
            or git_dir.parent != common / "worktrees"
            or not (git_dir / "gitdir").is_file()
            or Path((git_dir / "gitdir").read_text().strip()).resolve() != marker
            or _git(checkout, "branch", "--show-current") != binding.branch):
        raise ValueError(f"仓库副本工作树来源或分支不匹配：{checkout}")


def _prepare_copy(binding: RepositoryCopy) -> None:
    binding.checkout.parent.mkdir(parents=True, exist_ok=True)
    # 同一会话可能同时被前台和任务引擎初始化，跨线程/进程串行处理。
    with (binding.checkout.parent / f".{binding.checkout.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binding.checkout.exists() or binding.checkout.is_symlink():
            _verify_copy(binding)
            return  # 保留现有提交和未提交改动，不自动重置或拉取。
        normalize_repositories([str(binding.source)])
        _create_worktree(binding)
        _verify_copy(binding)


def _resume_worktree(binding: RepositoryCopy) -> bool:
    registered = _git(binding.source, "worktree", "list", "--porcelain", "-z")
    checkout = None
    for field in registered.split("\0"):
        if field.startswith("worktree "):
            checkout = Path(field.removeprefix("worktree "))
        elif field == f"branch refs/heads/{binding.branch}":
            if (checkout is None or checkout.parent != binding.checkout.parent
                    or not checkout.name.startswith(".initializing-")):
                raise ValueError(f"会话分支已被其它工作树使用：{binding.checkout}")
            _verify_copy(replace(binding, checkout=checkout))
            # 进程中断留下的工作树只移动不清空，保留其中可能存在的成果。
            _git(binding.source, "worktree", "move", str(checkout), str(binding.checkout))
            return True
    return False


def _create_worktree(binding: RepositoryCopy) -> None:
    if _resume_worktree(binding):
        return
    staging = Path(tempfile.mkdtemp(prefix=".initializing-", dir=binding.checkout.parent))
    try:
        # 只检出工作文件，共享本地 Git 历史；不复制对象，也不改原目录当前分支。
        branches = _git(binding.source, "for-each-ref", "--format=%(refname)", "refs/heads/")
        if f"refs/heads/{binding.branch}" in branches.splitlines():
            args = (str(staging), binding.branch)  # 上次初始化中断留下的会话分支。
        elif not _git(binding.source, "rev-parse", "--revs-only", "HEAD"):
            args = ("--orphan", "-b", binding.branch, str(staging))
        else:
            args = ("-b", binding.branch, str(staging), "HEAD")
        _git(binding.source, "worktree", "add", *args)
        _git(binding.source, "worktree", "move", str(staging), str(binding.checkout))
    finally:
        _cleanup_staging(binding, staging)


def _cleanup_staging(binding: RepositoryCopy, staging: Path) -> None:
    if not staging.exists():
        return
    try:
        # 只清理已完整检出且无改动的本次临时副本；不强解锁或删除半成品。
        if (staging / ".git").is_file():
            _verify_copy(replace(binding, checkout=staging))
            if _git(staging, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"):
                logging.getLogger(__name__).warning("仓库临时副本含改动，现场保留：%s", staging)
                return
            _git(binding.source, "worktree", "remove", str(staging))
        else:
            staging.rmdir()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        logging.getLogger(__name__).warning("仓库副本清理未完成，现场保留：%s", staging, exc_info=True)


def _authorization_target(session_key: str | None, source: str) -> tuple[str, bool]:
    from ..memory import agents

    owner = agents.agent_for_session(session_key) if session_key else None
    if not owner or not source.strip() or not Path(source).is_absolute():
        raise ValueError("需要有效会话及仓库原目录的绝对路径")
    requested = normalize_repositories([source])[0]
    if Path(requested).is_relative_to(config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories"):
        raise ValueError("请指定原仓库，不能把其它会话的副本作为来源")
    return requested, requested in session_sources(owner, session_key)


async def authorize_source(session_key: str | None, source: str) -> tuple[str, str]:
    from ..tools import danger
    from . import permissions

    requested, available = await asyncio.to_thread(_authorization_target, session_key, source)
    if available:
        return requested, ""
    was_full = permissions.is_full(session_key)
    if not await danger.require_approval(
        "写入未关联仓库（授权本会话使用隔离副本）", requested,
    ):
        raise ValueError("未获仓库操作授权；未创建副本，无需到设置中关联仓库")
    if permissions.is_full(session_key):
        return requested, "full"
    if was_full:
        raise ValueError("完全访问已失效，请按当前权限重新授权")
    return requested, "approved"


def prepare_for_session(session_key: str | None, source: str, authorization: str = "") -> RepositoryCopy:
    from ..memory import agents, session_store
    from . import permissions
    from .repository_locks import lease, session_digest

    owner = agents.agent_for_session(session_key) if session_key else None
    if not owner or not source.strip():
        raise ValueError("需要有效会话及仓库的原目录路径")
    requested = Path(source).expanduser().resolve()
    if str(requested) != source:
        raise ValueError("仓库路径在授权后发生变化，请重新准备")
    with lease(owner["id"], session_digest(session_key)):
        if authorization == "full" and not permissions.is_full(session_key):
            raise ValueError("完全访问已失效，请按当前权限重新授权")
        if str(requested) not in session_sources(owner, session_key) and authorization not in ("full", "approved"):
            raise ValueError("尚未获得该仓库的会话授权；未创建或修改任何副本")
        binding = copy_for_source(owner["id"], session_key, str(requested))
        _prepare_copy(binding)
        if authorization:
            session_store.grant_repository(session_key, owner["id"], str(requested), authorization)
        return binding


async def prepare_repositories(policy) -> None:
    for binding in policy.repositories:
        await asyncio.to_thread(_prepare_copy, binding)


def prompt_note(copies: tuple[RepositoryCopy, ...]) -> str:
    guidance = (
        "代码开发默认由当前 Agent 自行完成。Git 仓库无需单独绑定，关联目录会自动识别。"
        "没有关联目录不代表禁止开发：标准权限下写未关联目录需授权，完全访问无需绑定或重复审批。"
        "修改、构建或提交任何目标仓库前，直接调用 prepare_repository(path=原仓库绝对路径)；"
        "工具负责按当前权限授权并准备隔离副本，不要先要求主人到设置里关联仓库。"
        "并发开发默认在本地仓库创建会话分支和独立工作目录（git worktree），无需完整克隆；未列出的仓库也按此处理，不能把开发隔离当作权限拒绝。"
        "修改前读取目标仓库的 AGENTS.md/CLAUDE.md，保留已有未提交修改。"
        "业务部署沿用仓库部署脚本或 CI/CD；独立 HTML 报告分享仍走 publish_website。"
        "推送、发布按当前有效权限处理，不能一律宣称需要审批。"
    )
    if not copies:
        return guidance
    rows = "\n".join(f"- 原目录：{x.source} → 会话副本：{x.checkout}（分支 {x.branch}）" for x in copies)
    return (
        "\n## 本会话可用的 Git 仓库\n" + rows + "\n" + guidance
        + "这里只列出副本位置，不代表已经创建。普通聊天、读取资料、运行已有技能无需准备副本。"
        "需要修改、构建或提交关联仓库时，先调用 prepare_repository(path=原目录)，"
        "成功后使用返回的副本路径；失败不得使用未完成的目录。"
        "修改、构建及本地提交默认在会话副本进行，这不是原目录只读的权限限制。"
        "新工作目录共享本地 Git 历史和远程配置，只检出已提交文件，不复制历史、不带原目录未提交或忽略的文件（如 .env、node_modules），也不会自动同步远端；已有完整副本原地保留。"
        "缺少依赖或部署配置先说明，不得猜测或外发凭据。"
    )
