#!/usr/bin/env python3
"""仅准备 MCP OAuth 代理私有文件；不加载服务、不创建隧道、不运行下载物。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "deploy" / "mcp-auth"
VERSION = "v2.10.2"
ASSET = "mcp-auth-proxy-darwin-amd64"
RELEASE_API = f"repos/sigbit/mcp-auth-proxy/releases/tags/{VERSION}"
ASSET_URL = f"https://github.com/sigbit/mcp-auth-proxy/releases/download/{VERSION}/{ASSET}"
LABEL = "org.vococo.mcp-auth"
BINARY = "mcp-auth-proxy"
BCRYPT = re.compile(r"\$2[aby]\$12\$[./A-Za-z0-9]{53}")
MAX_BINARY = 128 * 1024 * 1024


class SetupError(Exception):
    """可安全展示、无需包含底层工具输出的错误。"""


def checked_root(value: str) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute() or root != root.resolve():
        raise SetupError("私有目录必须是绝对路径，且不能包含软链接或 ..")
    if root == REPO or REPO in root.parents:
        raise SetupError("凭据目录不能位于仓库中")
    if not root.parent.is_dir():
        raise SetupError("请先由用户建立私有目录的父目录")
    parent = root.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise SetupError("父目录必须属于当前用户，且不可被其他用户写入")
    return root


def check_private(path: Path, directory: bool = False, executable: bool = False) -> None:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    modes = {0o700} if directory else ({0o600, 0o700} if executable else {0o600})
    if (not expected_type(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in modes or (not directory and info.st_nlink != 1)):
        raise SetupError("发现非私有权限、软链接、硬链接或不属于当前用户的文件")


def write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_service(label: str = LABEL) -> None:
    if platform.system() != "Darwin":
        return
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True, check=False,
    )
    if result.returncode == 0:
        raise SetupError("同名 launchd 服务已加载；准备脚本不会覆盖或重载服务")


def hidden_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise SetupError("必须在用户本地交互终端运行，禁止通过参数或管道提供口令")
    return getpass.getpass(prompt)


def password_hash() -> str:
    password = hidden_input("设置登录口令（至少16字节，最多72字节）: ")
    if not 16 <= len(password.encode()) <= 72 or any(c in password for c in "\r\n\0"):
        raise SetupError("口令长度不合要求，或含换行/空字符")
    if password != hidden_input("再次输入登录口令: "):
        raise SetupError("两次口令不一致")
    try:
        import bcrypt
    except ImportError:
        return htpasswd_hash(password)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def htpasswd_hash(password: str) -> str:
    executable = Path("/usr/sbin/htpasswd")
    if not executable.is_file():
        raise SetupError("缺少 bcrypt 或系统 htpasswd；未安装任何依赖")
    result = subprocess.run(
        [str(executable), "-niBC", "12", "mcp"], input=password + "\n",
        text=True, capture_output=True, check=False,
    )
    hashed = result.stdout.strip().removeprefix("mcp:")
    if result.returncode or not BCRYPT.fullmatch(hashed):
        raise SetupError("系统 htpasswd 未生成预期 bcrypt 摘要；未展示其输出")
    return hashed


def make_env(root: Path) -> bytes:
    from vococo.deployment_settings import brain_origin
    origin = brain_origin()
    hashed = password_hash()
    token = hidden_input("输入已为 MCP frontend 配置的共享 bearer token（不会自动改上游）: ")
    if not re.fullmatch(r"[A-Za-z0-9._~+/\-]{32,4096}=*", token):
        raise SetupError("共享 token 必须至少32字符，且符合 bearer token 字符集")
    values = {
        "EXTERNAL_URL": origin, "NO_AUTO_TLS": "true",
        "LISTEN": "127.0.0.1:8853", "PASSWORD_HASH": hashed,
        "DATA_PATH": str(root / "data"), "REPOSITORY_BACKEND": "local",
        "PROXY_BEARER_TOKEN": token, "PROXY_FORWARD_AUTHORIZATION": "false",
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()).encode()


def render_files(root: Path, env: bytes, label: str = LABEL) -> dict[str, bytes]:
    runner = (TEMPLATES / "run-proxy.sh.in").read_text()
    for name, value in {"ENV": root / "proxy.env", "BINARY": root / BINARY}.items():
        runner = runner.replace(f"@{name}@", shlex.quote(str(value)))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", label):
        raise SetupError("launchd 标签格式无效")
    templates = list(TEMPLATES.glob("*.plist.in"))
    if len(templates) != 1:
        raise SetupError("需要唯一的 MCP launchd 模板")
    plist = plistlib.loads(templates[0].read_bytes())
    plist["Label"] = label
    plist["ProgramArguments"][-1] = str(root / "run-proxy.sh")
    plist["WorkingDirectory"] = str(root)
    return {"proxy.env": env, "run-proxy.sh": runner.encode(),
            label + ".plist": plistlib.dumps(plist)}


def verify_prepared(root: Path) -> None:
    check_private(root, directory=True)
    check_private(root / "data", directory=True)
    check_private(root / "prepared.json")
    manifest = json.loads((root / "prepared.json").read_text())
    names = set(manifest.get('files', {}))
    plists = [name for name in names if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*\.plist', name)]
    if len(plists) != 1 or names != {'proxy.env', 'run-proxy.sh', plists[0]}:
        raise SetupError('准备目录的文件清单无效')
    label = plists[0].removesuffix('.plist')
    if manifest.get("version") != VERSION or set(manifest.get("files", {})) != names:
        raise SetupError("不是完整的固定版本准备目录；拒绝覆盖")
    for name, expected in manifest["files"].items():
        check_private(root / name)
        if digest_file(root / name) != expected:
            raise SetupError("已有配置发生变更；拒绝覆盖凭据或服务文件")
    expected = render_files(root, (root / "proxy.env").read_bytes(), label)
    if any((root / name).read_bytes() != content for name, content in expected.items()):
        raise SetupError("模板或路径发生变更；请人工审核，脚本不自动更新服务")


def prepare(root: Path, label: str = LABEL) -> bool:
    if root.exists():
        verify_prepared(root)
        manifest = json.loads((root / 'prepared.json').read_text())
        existing = next(name.removesuffix('.plist') for name in manifest['files'] if name.endswith('.plist'))
        check_service(existing)
        return False
    check_service(label)
    # 获取并验证输入后再创建目录，避免口令错误留下半成品。
    files = render_files(root, make_env(root), label)
    root.mkdir(mode=0o700)
    (root / "data").mkdir(mode=0o700)
    for name, content in files.items():
        write_private(root / name, content)
    manifest = {"version": VERSION, "files": {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }}
    write_private(root / "prepared.json", json.dumps(manifest).encode())
    return True


def release_asset(release: dict) -> dict:
    if release.get("tag_name") != VERSION or release.get("draft") or release.get("prerelease"):
        raise SetupError("不是指定正式版本")
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == ASSET]
    if len(assets) != 1:
        raise SetupError("官方发布缺少唯一 darwin-amd64 asset")
    asset = assets[0]
    if (asset.get("browser_download_url") != ASSET_URL
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "")):
        raise SetupError("官方 asset 缺少 SHA-256 digest 或 URL 不匹配；不能验证，拒绝下载")
    return asset


def fetch_release() -> dict:
    result = subprocess.run(
        ["gh", "api", RELEASE_API], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise SetupError("无法读取 GitHub 官方 release 元数据；未展示工具输出")
    return json.loads(result.stdout)


def download_verified(root: Path) -> bool:
    verify_prepared(root)
    if platform.system() != "Darwin" or platform.machine() != "x86_64":
        raise SetupError("只支持已确认的 Darwin x86_64 主机")
    asset = release_asset(fetch_release())
    expected = asset["digest"].removeprefix("sha256:")
    destination = root / BINARY
    if destination.exists() or destination.is_symlink():
        check_private(destination, executable=True)
        if digest_file(destination) != expected:
            raise SetupError("已有二进制摘要不同；拒绝覆盖，请另建版本目录人工更新")
        return False
    fetch_binary(root, destination, expected)
    return True


def fetch_binary(root: Path, destination: Path, expected: str) -> None:
    with tempfile.NamedTemporaryFile(dir=root, prefix=".download-", delete=False) as output:
        temporary = Path(output.name)
        try:
            with urllib.request.urlopen(ASSET_URL, timeout=60) as response:
                if not response.geturl().startswith("https://"):
                    raise SetupError("下载最终地址不是 HTTPS")
                copy_binary(response, output)
            output.flush()
            if digest_file(temporary) != expected:
                raise SetupError("二进制 SHA-256 与 GitHub asset digest 不一致；拒绝保存")
            # link 不覆盖并发创建的目标；下载文件保持600，绝不执行。
            os.link(temporary, destination)
        finally:
            temporary.unlink()


def copy_binary(response, output) -> None:
    size = 0
    while chunk := response.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_BINARY:
            raise SetupError("下载超过大小限制")
        output.write(chunk)
    if not size:
        raise SetupError("下载为空")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "download"])
    parser.add_argument("--private-dir", required=True, help="仓库外私有目录；无默认HOME写入")
    parser.add_argument("--label", default=LABEL, help="launchd 标签；旧准备目录保留原标签")
    args = parser.parse_args(argv)
    try:
        root = checked_root(args.private_dir)
        changed = prepare(root, args.label) if args.action == "prepare" else download_verified(root)
    except SetupError as error:
        print(f"准备失败：{error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # 底层异常可能包含凭据文件正文或外部工具输出，不能直接打印。
        print("准备失败：文件/元数据格式或外部工具异常；未覆盖已有文件。请参阅操作说明。", file=sys.stderr)
        return 1
    print("准备完成。" if changed else "已准备，未改动已有文件。")
    print("未安装或启动服务，未配置DNS/隧道；外网开放前必须配置并验证Cloudflare登录限流。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
