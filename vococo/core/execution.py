"""集中解析 Agent 的执行目录，成果目录与项目身份保持不变。"""
from __future__ import annotations

import os
import subprocess
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import business_repositories
from .business_repositories import RepositoryCopy, prepare_repositories


def execution_mode(workdir: str | None, explicit: str | None = None) -> str:
    """显式策略优先；iCloud 或非 Git 目录默认使用本地业务空间。"""
    if explicit in ("workspace", "project"):
        return explicit
    if not workdir:
        return "workspace"
    path = Path(workdir).expanduser().resolve()
    # 软链也按真实路径判断，避免对 iCloud 目录运行 Git。
    if "Mobile Documents" in path.parts or "com~apple~CloudDocs" in path.parts:
        return "workspace"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=path,
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "workspace"
    return "project" if result.returncode == 0 and result.stdout.strip() == "true" else "workspace"


@dataclass(frozen=True)
class ExecutionPolicy:
    """业务执行目录及后续权限集成所需的可写目录，不代表已授予权限。"""

    agent_id: str
    workspace: Path
    output_dir: Path
    writable_roots: tuple[Path, ...]
    source_dir: Path | None = None
    repositories: tuple[RepositoryCopy, ...] = ()


def policy_for_session(
    session_key: str | None, cwd: str | None = None,
) -> ExecutionPolicy | None:
    """业务 Agent 返回策略并创建 workspace/tmp；开发 Agent、总助理返回 None。

    cwd 仅为调用方兼容参数，业务 Agent 不因显式或历史 cwd 改变身份与执行目录。
    成果目录及关联目录只登记，不创建、不写入。
    """
    if not session_key:
        return None
    from ..memory import agents

    agent = agents.agent_for_session(session_key)
    if not agent or agent["id"] == agents.GENERAL_ID or agent["execution_mode"] != "workspace":
        return None
    workspace = (config.AGENT_WORKSPACES_DIR / agent["id"]).expanduser().resolve()
    output_dir = Path(agent.get("output_dir") or agent["workdir"]).expanduser().resolve()
    roots = [workspace, output_dir, agents.home(agent["id"]).resolve()]
    roots.extend(Path(link["path"]).resolve() for link in agent["links"] if link["writable"])
    own_home = agents.home(agent["id"]).resolve()
    for path in roots:
        if path != own_home and any((p / ".git").exists() for p in (path, *path.parents)):
            raise ValueError(f"业务可写目录不能位于代码仓库，请配置仓库外的成果目录：{path}")
    repositories = business_repositories.copies_for_session(agent, session_key)
    roots.extend(binding.checkout for binding in repositories)
    (workspace / "tmp").mkdir(parents=True, exist_ok=True)
    return ExecutionPolicy(agent["id"], workspace, output_dir, tuple(dict.fromkeys(roots)),
                           Path(agent["workdir"]).expanduser().resolve(), repositories)


def prompt_note(policy: ExecutionPolicy) -> str:
    """成果目录是显式目的地，不让相对路径草稿冒充已交付成果。"""
    return (
        "\n\n## 业务文件目录\n"
        f"运行目录：{policy.workspace}\n临时文件：{policy.workspace / 'tmp'}\n"
        f"正式成果目录：{policy.output_dir}\n原项目/技能资源目录（只作读取来源）：{policy.source_dir or policy.output_dir}\n"
        "原规则中的 scripts/ 等相对命令必须改用原资源目录的绝对路径，不要从运行目录猜测。"
        "报告、表格等正式成果必须用绝对路径写到正式成果目录；相对路径仅用于临时文件。"
        "成果目录不可读写时明确报错，不得改存源码仓库或把临时副本报告成已交付。"
    ) + business_repositories.prompt_note(policy.repositories)


def network_settings() -> dict:
    """复用已有本机 HTTP 代理；普通 TCP 仍经 SDK 的 SOCKS 代理。"""
    network = {"allowedDomains": ["*"]}
    proxy = next((os.environ[name] for name in
                  ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")
                  if os.environ.get(name)), "")
    try:
        url = urlsplit(proxy)
        if (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
                and not url.username and not url.password):
            port = url.port if url.port is not None else 80
            if port > 0:
                network["httpProxyPort"] = port
    except ValueError:
        pass  # 无法复用时仍保留 SDK 默认代理，不放宽网络或文件边界。
    return network


def shell_env() -> dict[str, str]:
    """脚本内 subprocess 调用 ssh 时也使用 SDK 注入的代理参数。"""
    adapters = Path(__file__).parent / "sandbox_bin"
    return {"PATH": str(adapters) + os.pathsep + os.environ.get("PATH", os.defpath)}


def sandbox_settings(policy: ExecutionPolicy) -> dict:
    """让 shell 及其 Python/子进程共用系统写入边界，而非猜测命令字符串。"""
    import tempfile
    from ..memory import agents

    roots = [*policy.writable_roots, config.AI_BRAIN_DIR.resolve(),
             Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()]
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "filesystem": {
            "allowWrite": [str(p) for p in dict.fromkeys(roots)],
            "denyWrite": [str(agents.home(policy.agent_id).resolve() / "agent.json"),
                          str(config.CRON_JOBS_PATH.resolve()),
                          *(str(binding.source) for binding in policy.repositories)],
        },
        "network": network_settings(),
    }
