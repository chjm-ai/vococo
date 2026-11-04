"""关联目录自动识别 Git 仓库，每会话使用独立副本避免并发覆盖。"""
from __future__ import annotations

import asyncio
import hashlib
import fcntl
import shutil
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


def copies_for_session(agent: dict, session_key: str) -> tuple[RepositoryCopy, ...]:
    session = hashlib.sha256(session_key.encode()).hexdigest()[:20]
    base = config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories" / agent["id"] / session
    copies = []
    for source in repository_sources(agent):
        root = Path(source).expanduser().resolve()
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        checkout = base / digest
        if checkout.absolute() != checkout.resolve():
            raise ValueError(f"仓库副本路径不能经过软链接：{checkout}")
        copies.append(RepositoryCopy(root, checkout, f"vococo/business-{session}-{digest}"))
    return tuple(copies)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60,
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
        if staging.exists():
            # 只清理本次新建的临时目录和登记，绝不删除已暴露给会话的工作成果。
            if (staging / ".git").is_file():
                _git(binding.source, "worktree", "remove", "--force", str(staging))
            else:
                shutil.rmtree(staging)


async def prepare_repositories(policy) -> None:
    for binding in policy.repositories:
        await asyncio.to_thread(_prepare_copy, binding)


def prompt_note(copies: tuple[RepositoryCopy, ...]) -> str:
    guidance = (
        "代码开发默认由当前 Agent 自行完成。Git 仓库无需单独绑定，关联目录会自动识别。"
        "没有关联目录不代表禁止开发：标准权限下写未关联目录需授权，完全访问无需绑定或重复审批。"
        "并发开发默认在本地仓库创建会话分支和独立工作目录（git worktree），无需完整克隆；未列出的仓库也按此处理，不能把开发隔离当作权限拒绝。"
        "修改前读取目标仓库的 AGENTS.md/CLAUDE.md，保留已有未提交修改。"
        "业务部署沿用仓库部署脚本或 CI/CD；独立 HTML 报告分享仍走 publish_website。"
        "推送、发布按当前有效权限处理，不能一律宣称需要审批。"
    )
    if not copies:
        return guidance
    rows = "\n".join(f"- 原目录：{x.source} → 会话副本：{x.checkout}（分支 {x.branch}）" for x in copies)
    return (
        "\n## 关联目录中的 Git 仓库\n" + rows + "\n" + guidance
        + "修改、构建及本地提交默认在会话副本进行，这不是原目录只读的权限限制。"
        "新工作目录共享本地 Git 历史和远程配置，只检出已提交文件，不复制历史、不带原目录未提交或忽略的文件（如 .env、node_modules），也不会自动同步远端；已有完整副本原地保留。"
        "缺少依赖或部署配置先说明，不得猜测或外发凭据。"
    )
