"""业务仓库绑定：主检出只读，每会话使用不共享 Git 元数据的独立副本。"""
from __future__ import annotations

import asyncio
import hashlib
import os
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


def copies_for_session(agent: dict, session_key: str) -> tuple[RepositoryCopy, ...]:
    session = hashlib.sha256(session_key.encode()).hexdigest()[:20]
    base = config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories" / agent["id"] / session
    copies = []
    for source in agent.get("repositories", []):
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


def _verify_copy(binding: RepositoryCopy) -> None:
    checkout = binding.checkout
    if checkout.absolute() != checkout.resolve() or not (checkout / ".git").is_dir():
        raise ValueError(f"仓库副本必须有独立 Git 目录：{checkout}")
    common = Path(_git(checkout, "rev-parse", "--git-common-dir"))
    common = (checkout / common).resolve()
    if common != checkout / ".git" or (common / "objects/info/alternates").exists():
        raise ValueError(f"仓库副本不能共享 Git 元数据或对象：{checkout}")
    if Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError(f"仓库副本根目录不匹配：{checkout}")
    if _git(checkout, "config", "--get", "vococo.source") != str(binding.source):
        raise ValueError(f"仓库副本初始化未完成或来源不匹配，请检查：{checkout}")


def _prepare_copy(binding: RepositoryCopy) -> None:
    if binding.checkout.exists() or binding.checkout.is_symlink():
        _verify_copy(binding)
        return  # 保留现有提交和未提交改动，不自动重置或拉取。
    normalize_repositories([str(binding.source)])
    binding.checkout.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".initializing-", dir=binding.checkout.parent))
    try:
        _clone_copy(replace(binding, checkout=staging))
        try:
            os.rename(staging, binding.checkout)
        except OSError:
            if not binding.checkout.exists():
                raise
            _verify_copy(binding)  # 另一个初始化进程先完成时只复用已验证的副本。
    finally:
        if staging.exists():
            shutil.rmtree(staging)  # 只清理本次创建、尚未暴露给会话的临时副本。


def _clone_copy(binding: RepositoryCopy) -> None:
    # --no-local 使用 Git 传输协议，避免本地克隆的硬链接/共享对象越过沙箱。
    _git(binding.source, "clone", "--no-local", "--", str(binding.source), str(binding.checkout))
    remotes = _git(binding.source, "remote").splitlines()
    origin = _git(binding.source, "remote", "get-url", "origin") if "origin" in remotes else ""
    _git(binding.checkout, "remote", "remove", "origin")
    if origin:
        _git(binding.checkout, "remote", "add", "origin", origin)
    _git(binding.checkout, "switch", "-c", binding.branch)
    _git(binding.checkout, "config", "vococo.source", str(binding.source))
    _verify_copy(binding)


async def prepare_repositories(policy) -> None:
    for binding in policy.repositories:
        await asyncio.to_thread(_prepare_copy, binding)


def prompt_note(copies: tuple[RepositoryCopy, ...]) -> str:
    if not copies:
        return "需要修改代码仓库请先由主人在 Agent 设置中绑定仓库，不要关闭沙箱。"
    rows = "\n".join(f"- 只读原仓库：{x.source} → 可写副本：{x.checkout}（分支 {x.branch}）" for x in copies)
    return (
        "\n## 已绑定代码仓库\n" + rows + "\n"
        "修改前先读取对应副本的 AGENTS.md/CLAUDE.md，规则仅用于该仓库。"
        "修改、构建、本地提交必须在上述可写副本操作；任务给出的原仓库路径也要换成副本路径。"
        "不要写原仓库、不要直接操作原仓库主分支。副本只含已提交内容，不带原目录的未提交修改、"
        ".env 或 node_modules；缺少依赖或部署配置时先说明，不得猜测或外发凭据。"
        "副本不会自动同步远端，继续开发前先检查分支和远端状态。推送、发布仍走原有审批，"
        "无人批准就停在本地提交，不得因绑定仓库跳过审批。"
    )
