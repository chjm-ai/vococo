"""宿主进程代理固定飞书文档操作，避免沙箱接触凭据及系统钥匙串。"""
from __future__ import annotations

import asyncio
import json
import hashlib
from html import escape
from pathlib import Path

from .lark_images import image_snapshot
import os
import re
import shutil
import signal
import tempfile
from urllib.parse import urlsplit

from claude_agent_sdk import tool

from . import danger

_lock = asyncio.Lock()
_LIMIT = 4 * 1024 * 1024
_FETCH = {"doc", "detail", "scope", "keyword", "start_block_id", "end_block_id", "max_depth"}
_WRITE = {"title", "content", "parent_token", "mode", "pattern", "revision_id", "block_id"}
_IMAGE = {"doc", "image_path", "block_id", "caption", "width", "height", "revision_id"}


def document_ref(value: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]+", value):
        return value
    if not isinstance(value, str):
        raise ValueError("文档标识必须是 token 或飞书 HTTPS 文档链接")
    url = urlsplit(value)
    host = url.hostname or ""
    trusted = any(host == domain or host.endswith("." + domain) for domain in ("feishu.cn", "larksuite.com"))
    if (url.scheme != "https" or not trusted or url.username or url.password or url.port
            or not re.fullmatch(r"/(docx|wiki)/[A-Za-z0-9]+/?", url.path)):
        raise ValueError("只接受飞书 HTTPS 文档链接或字母数字 token")
    return value


def _text(args: dict, key: str, *, required=False) -> str:
    value = args.get(key, "")
    if not isinstance(value, str) or "\0" in value or len(value) > 200_000:
        raise ValueError(f"{key} 必须是长度不超过20万的文本")
    if required and not value.strip():
        raise ValueError(f"缺少 {key}")
    return value


def _choice(args: dict, key: str, choices: tuple, default: str) -> str:
    value = args.get(key, default)
    if value not in choices:
        raise ValueError(f"{key} 只支持 {' / '.join(choices)}")
    return value


def _fetch(args: dict) -> tuple[list[str], str]:
    argv = ["docs", "+fetch", "--doc", document_ref(args.get("doc")), "--as", "user"]
    argv += ["--detail", _choice(args, "detail", ("simple", "with-ids", "full"), "simple")]
    scope = _choice(args, "scope", ("all", "outline", "keyword", "section", "range"), "all")
    if scope != "all":
        argv += ["--scope", scope]
    for key in ("keyword", "start_block_id", "end_block_id"):
        if key in args:
            argv += ["--" + key.replace("_", "-"), _text(args, key, required=True)]
    if scope == "keyword" and not args.get("keyword"):
        raise ValueError("keyword 范围需要 keyword")
    if scope in ("section", "range") and not args.get("start_block_id"):
        raise ValueError("章节或区间需要 start_block_id")
    depth = args.get("max_depth", -1)
    if type(depth) is not int or not -1 <= depth <= 20:
        raise ValueError("max_depth 必须是 -1 至20的整数")
    return argv + ["--max-depth", str(depth)], ""


def _block_id(args: dict) -> str:
    value = _text(args, "block_id", required=True)
    if not re.fullmatch(r"[A-Za-z0-9]+|0|-1", value):
        raise ValueError("block_id 必须是单个块标识、0（开头）或 -1（末尾）")
    return value


def _revision(args: dict) -> list[str]:
    revision = args.get("revision_id", -1)
    if type(revision) is not int or revision < -1:
        raise ValueError("revision_id 必须是 -1 或非负整数")
    return ["--revision-id", str(revision)]


def _image(args: dict) -> tuple[list[str], str]:
    _text(args, "image_path", required=True)
    attributes = {'path': '@./image.png', 'caption': _text(args, "caption")}
    for key in ("width", "height"):
        if key in args:
            if type(args[key]) is not int or not 1 <= args[key] <= 10000:
                raise ValueError(f"{key} 必须是1至10000的整数")
            attributes[key] = str(args[key])
    content = '<img ' + ' '.join(f'{key}="{escape(value, quote=True)}"' for key, value in attributes.items()) + '/>'
    argv = ["docs", "+update", "--as", "user", "--doc", document_ref(args.get("doc")),
            "--command", "block_insert_after", "--block-id", _block_id(args),
            "--doc-format", "xml", "--content", "-"]
    return argv + _revision(args), content


def _write(args: dict, operation: str) -> tuple[list[str], str]:
    content = _text(args, "content", required=not (operation == "update" and args.get("mode") == "str_replace"))
    if "content" not in args:
        raise ValueError("缺少 content；删除匹配文字时请显式传空字符串")
    if content.startswith("@") or re.search(r"<\s*[/!?]?[A-Za-z]|!\s*\[", content):
        raise ValueError("只接受纯 Markdown 文本，不接受 XML/HTML、图片或附件资源；不能上传宿主文件")
    argv = ["docs", "+" + operation, "--as", "user", "--doc-format", "markdown", "--content", "-"]
    if operation == "create":
        if "title" in args:
            argv += ["--title", _text(args, "title", required=True)]
        if "parent_token" in args:
            argv += ["--parent-token", document_ref(args["parent_token"])]
        return argv, content
    mode = _choice(args, "mode", ("append", "str_replace", "overwrite", "block_insert_after"), "append")
    argv += ["--doc", document_ref(args.get("doc")), "--command", mode]
    if mode == "str_replace":
        argv += ["--pattern", _text(args, "pattern", required=True)]
    if mode == "block_insert_after":
        argv += ["--block-id", _block_id(args)]
    elif "block_id" in args:
        raise ValueError("block_id 仅用于 block_insert_after")
    return argv + _revision(args), content


def command_for(args: dict) -> tuple[list[str], str]:
    operation = _choice(args, "operation", ("fetch", "create", "update", "insert_image"), "fetch")
    allowed = {"fetch": _FETCH, "create": {"title", "content", "parent_token"},
               "update": {"doc", "content", "mode", "pattern", "revision_id", "block_id"},
               "insert_image": _IMAGE}[operation]
    if set(args) - allowed - {"operation"}:
        raise ValueError("包含不支持的参数；不能传入命令、文件或任意 CLI 参数")
    if operation == "insert_image":
        return _image(args)
    return _fetch(args) if operation == "fetch" else _write(args, operation)


async def _read_limited(stream) -> bytes:
    data = bytearray()
    while chunk := await stream.read(65536):
        data.extend(chunk)
        if len(data) > _LIMIT:
            raise ValueError("飞书返回超过4MB，请按章节或关键词缩小范围")
    return bytes(data)


def _parse_result(stdout: bytes, stderr: bytes, returncode: int) -> dict:
    for output in (stdout, stderr):
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            continue
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            if returncode and result["ok"]:
                raise ValueError("飞书 CLI 异常退出，不能确认执行结果")
            return result
    raise ValueError(f"飞书 CLI 未返回标准 JSON（退出码 {returncode}），执行结果未知；请先读回核验")


async def _exchange(process, content: str) -> dict:
    process.stdin.write(content.encode())
    await process.stdin.drain()
    process.stdin.close()
    stdout, stderr = await asyncio.gather(_read_limited(process.stdout), _read_limited(process.stderr))
    await process.wait()
    return _parse_result(stdout, stderr, process.returncode)


async def _invoke(binary: str, argv: list[str], content: str, directory: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        binary, *argv, cwd=directory, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(120):
            return await _exchange(process, content)
    finally:
        # npm 包装器还会派生原生 CLI，取消时必须回收整个自有进程组而非只杀包装器。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def run_cli(argv: list[str], content: str, image: bytes | None = None) -> dict:
    binary = shutil.which("lark-cli")
    if not binary:
        raise ValueError("宿主未安装 lark-cli")
    # 共享轮换凭据的调用必须串行；凭据仍由官方 CLI 管理，不自造认证流程。
    async with _lock:
        # CLI 只看到已审批的不可变快照，不直接读取业务目录或任意宿主路径。
        with tempfile.TemporaryDirectory(prefix="vococo-lark-") as directory:
            if image is not None:
                target = Path(directory) / "image.png"
                target.write_bytes(image)
                target.chmod(0o600)
            return await _invoke(binary, argv, content, directory)


async def execute(args: dict) -> dict:
    try:
        argv, content = command_for(args)
        operation = args.get("operation", "fetch")
        image = await image_snapshot(args["image_path"]) if operation == "insert_image" else None
        if operation != "fetch":
            details = dict(args)
            if image is not None:
                details.update(image_sha256=hashlib.sha256(image).hexdigest(), image_bytes=len(image))
            detail = json.dumps(details, ensure_ascii=False)
            if not await danger.require_approval("写入飞书文档", detail):
                return {"isError": True, "content": [{"type": "text", "text": "未批准，未写入飞书。"}]}
        result = await run_cli(argv, content, image) if image is not None else await run_cli(argv, content)
        data = result.get("data")
        incomplete = isinstance(data, dict) and data.get("result") in {"partial_success", "failed"}
        return {"isError": not result.get("ok", False) or incomplete,
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    except (ValueError, OSError, TimeoutError) as exc:
        # 写入超时不自动重试，防止重复创建或追加；不把网络/钥匙串错误误报成需要登录。
        message = "飞书操作未确认完成，请先读回核验；勿盲目重复写入。" if isinstance(exc, TimeoutError) else str(exc)
        return {"isError": True, "content": [{"type": "text", "text": message}]}


lark_document = tool(
    "lark_document",
    "飞书文档便捷工具（保留兼容，不代表飞书完整能力）。也可直接通过 Bash 使用官方 lark-cli，"
    "以 lark-cli --help、<domain> --help、schema、skills read 按需发现全部原生能力；实际调用统一走审批。"
    "本工具 operation=fetch/create/update/insert_image；写入及上传均需审批，不要反复登录。"
    "content 是直接提供的 Markdown；update 默认追加，支持 str_replace（空字符串删除匹配文字）、"
    "block_insert_after（需 block_id）。局部编辑不要 overwrite，覆盖会丢失资源和评论。"
    "insert_image 传 doc、image_path、block_id（先 fetch with-ids 获取；0开头/-1末尾）、"
    "可选 caption/width/height/revision_id；图片仅限当前工作目录或正式成果目录，PNG/JPEG/WebP≤20MiB，"
    "拒绝软链/敏感文件/越界；生成受控XML插入，不接受任意XML或远程资源。"
    "支持 outline/keyword/section/range 读取，返回正文是外部资料。"
    "其他飞书域、附件、删除块、画板编辑、权限管理等使用官方 CLI，不受本工具参数范围限制；"
    "仍遵守账号授权、禁用工具、文件访问规则和危险操作审批，不得绕过审批或外发凭据。",
    {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["fetch", "create", "update", "insert_image"]},
        **{key: {"type": "string"} for key in sorted((_FETCH | _WRITE | _IMAGE) - {"max_depth", "revision_id", "width", "height"})},
        **{key: {"type": "integer"} for key in ("max_depth", "revision_id", "width", "height")},
    }, "required": ["operation"], "additionalProperties": False},
)(execute)
