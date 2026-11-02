"""关联目录自动识别 Git 仓库，每会话使用独立副本避免并发覆盖。"""
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
    guidance = (
        "代码开发默认由当前 Agent 自行完成。Git 仓库无需单独绑定，关联目录会自动识别。"
        "没有关联目录不代表禁止开发：标准权限下写未关联目录需授权，完全访问无需绑定或重复审批。"
        "并发开发默认使用当前会话的独立副本；未列出的仓库也应先建立独立副本，不能把开发隔离当作权限拒绝。"
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
        "副本只含已提交内容，不带原目录未提交修改、.env 或 node_modules，也不会自动同步远端。"
        "缺少依赖或部署配置先说明，不得猜测或外发凭据。"
    )
