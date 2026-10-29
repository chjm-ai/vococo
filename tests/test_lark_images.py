"""图片导入和局部编辑的受控回归。"""
import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from vococo.tools import lark_documents as lark


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_image_command_is_generated_xml_not_user_xml():
    argv, content = lark.command_for({"operation": "insert_image", "doc": "abc", "block_id": "blk123",
                                      "image_path": "test.png", "caption": '图"<&', "width": 320})
    assert argv[argv.index("--command") + 1] == "block_insert_after"
    assert argv[argv.index("--block-id") + 1] == "blk123"
    assert argv[argv.index("--doc-format") + 1] == "xml"
    assert 'path="@./image.png"' in content
    assert 'caption="图&quot;&lt;&amp;"' in content
    assert "overwrite" not in argv


def test_local_text_patch_and_empty_replacement():
    argv, content = lark.command_for({"operation": "update", "doc": "abc", "mode": "str_replace",
                                      "pattern": "重复文字", "content": ""})
    assert content == ""
    argv, _ = lark.command_for({"operation": "update", "doc": "abc", "mode": "block_insert_after",
                                "block_id": "blk123", "content": "新增段落"})
    assert "--block-id" in argv and "overwrite" not in argv


@pytest.mark.parametrize("extra", [{"width": 0}, {"height": True}, {"block_id": "@file"},
                                   {"mode": "overwrite"}, {"caption": "x" * 200001}])
def test_image_invalid_parameters(extra):
    with pytest.raises(ValueError):
        lark.command_for({"operation": "insert_image", "doc": "abc", "block_id": "blk123",
                          "image_path": "test.png", **extra})


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.anyio
async def test_image_denial_never_launches_or_uploads(monkeypatch):
    monkeypatch.setattr(lark, "image_snapshot", AsyncMock(return_value=image_bytes()))
    monkeypatch.setattr(lark.danger, "require_approval", AsyncMock(return_value=False))
    run = AsyncMock()
    monkeypatch.setattr(lark, "run_cli", run)
    result = await lark.execute({"operation": "insert_image", "doc": "abc", "block_id": "blk123",
                                 "image_path": "test.png"})
    assert result["isError"]
    run.assert_not_awaited()


@pytest.mark.anyio
async def test_image_approved_bytes_bound_and_timeout_not_retried(monkeypatch):
    payload = image_bytes()
    monkeypatch.setattr(lark, "image_snapshot", AsyncMock(return_value=payload))
    approve = AsyncMock(return_value=True)
    run = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(lark.danger, "require_approval", approve)
    monkeypatch.setattr(lark, "run_cli", run)
    result = await lark.execute({"operation": "insert_image", "doc": "abc", "block_id": "blk123",
                                 "image_path": "test.png"})
    assert result["isError"] and "勿盲目重复" in result["content"][0]["text"]
    assert "sha256" in approve.call_args.args[1]
    run.assert_awaited_once()
    assert run.call_args.args[2] == payload


@pytest.mark.anyio
async def test_real_staging_only_contains_approved_snapshot_and_is_cleaned(tmp_path, monkeypatch):
    from pathlib import Path

    binary = tmp_path / "cli"
    binary.write_text("#!/usr/bin/env python3\nimport json,pathlib,sys\n"
                      "p=pathlib.Path.cwd()\n"
                      "print(json.dumps({'ok':True,'files':[x.name for x in p.iterdir()],"
                      "'image':(p/'image.png').read_bytes().hex(),'cwd':str(p),'xml':sys.stdin.read()}))\n")
    binary.chmod(0o700)
    monkeypatch.setattr(lark.shutil, "which", lambda _: str(binary))
    lark._lock = asyncio.Lock()
    argv, content = lark.command_for({"operation": "insert_image", "doc": "abc", "block_id": "blk123",
                                      "image_path": "/never/pass/this/to/cli.png"})
    result = await lark.run_cli(argv, content, image_bytes())
    assert result["files"] == ["image.png"]
    assert bytes.fromhex(result["image"]) == image_bytes()
    assert not Path(result["cwd"]).exists()
    assert "/never/pass" not in result["xml"]


@pytest.mark.parametrize("kind", ["outside", "traversal", "symlink", "parent_symlink", "secret", "hidden", "hardlink", "invalid"])
@pytest.mark.anyio
async def test_image_file_boundaries(tmp_path, monkeypatch, kind):
    from vococo.tools import artifacts, lark_images
    from vococo.gateway import clarify
    from types import SimpleNamespace

    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "image.png"
    path.write_bytes(image_bytes())
    monkeypatch.setattr(artifacts, "_roots", lambda *_: (root,))
    monkeypatch.setattr(lark.danger, "current_cwd", lambda: str(root))
    monkeypatch.setattr(clarify, "current", lambda: SimpleNamespace(session_key="test"))
    if kind == "outside":
        path = tmp_path / "outside.png"
        path.write_bytes(image_bytes())
    elif kind == "traversal":
        path = root / ".." / "workspace" / "image.png"
    elif kind == "symlink":
        link = root / "link.png"
        link.symlink_to(path)
        path = link
    elif kind == "parent_symlink":
        link = root / "link"
        link.symlink_to(root, target_is_directory=True)
        path = link / "image.png"
    elif kind in {"secret", "hidden"}:
        path = root / ("credentials.png" if kind == "secret" else ".hidden.png")
        path.write_bytes(image_bytes())
    elif kind == "hardlink":
        (root / "hardlink.png").hardlink_to(path)
    else:
        path.write_bytes(b"not an image")
    with pytest.raises((ValueError, OSError)):
        await lark_images.image_snapshot(str(path))


@pytest.mark.anyio
async def test_workspace_and_formal_output_images_are_allowed(tmp_path, monkeypatch):
    from vococo.tools import artifacts, lark_images
    from vococo.gateway import clarify
    from types import SimpleNamespace

    roots = (tmp_path / "workspace", tmp_path / "output")
    for root in roots:
        root.mkdir()
        (root / "image.png").write_bytes(image_bytes())
    monkeypatch.setattr(artifacts, "_roots", lambda *_: roots)
    monkeypatch.setattr(lark.danger, "current_cwd", lambda: str(roots[0]))
    monkeypatch.setattr(clarify, "current", lambda: SimpleNamespace(session_key="test"))
    for root in roots:
        data = await lark_images.image_snapshot(str(root / "image.png"))
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (16, 16)


@pytest.mark.parametrize("filename,data", [("image.svg", b"<svg/>"), ("image.png", b""),
                                           ("image.png", b"x" * (20 * 1024 * 1024 + 1))])
def test_image_format_size_limits(filename, data):
    from vococo.tools.lark_images import normalize_image
    with pytest.raises(ValueError):
        normalize_image(filename, data)


def test_normalization_strips_metadata_and_trailing_payload():
    from PIL.PngImagePlugin import PngInfo
    from vococo.tools.lark_images import normalize_image

    buffer = io.BytesIO()
    metadata = PngInfo()
    metadata.add_text("private", "do not upload")
    Image.new("RGB", (16, 16)).save(buffer, format="PNG", pnginfo=metadata)
    clean = normalize_image("image.png", buffer.getvalue() + b"trailing payload")
    assert b"do not upload" not in clean and b"trailing payload" not in clean


@pytest.mark.parametrize("prefix", ["lark-cli", "lark-cli --profile test", "lark-cli --profile=test"])
def test_help_allowed_but_profile_cannot_bypass_native_approval(prefix):
    assert not lark.danger._lark_requires_approval(prefix + " docs +update --help")
    assert lark.danger._lark_requires_approval(prefix + " docs +update --doc abc --content -")
    # 内容中的 --help 不是只读帮助。
    assert lark.danger._lark_requires_approval(prefix + " docs +update --doc abc --content --help")


def test_phone_photo_orientation_survives_metadata_removal():
    from vococo.tools.lark_images import normalize_image

    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (30, 10), "blue").save(buffer, format="JPEG", exif=exif)
    clean = normalize_image("photo.jpg", buffer.getvalue())
    with Image.open(io.BytesIO(clean)) as image:
        assert image.size == (10, 30)
        assert not image.getexif()


@pytest.mark.anyio
async def test_actual_policy_roots_allow_own_outputs_but_not_other_sessions(tmp_path, monkeypatch):
    from vococo.tools import lark_images
    from vococo.gateway import clarify
    from vococo.core import execution
    from types import SimpleNamespace

    workspace, output, other = [tmp_path.resolve() / name for name in ("own", "output", "other-session")]
    for root in (workspace, output, other):
        root.mkdir()
        (root / "image.png").write_bytes(image_bytes())
    policy = execution.ExecutionPolicy("test-agent", workspace, output, ())
    monkeypatch.setattr(execution, "policy_for_session", lambda session, cwd: policy if session == "own-session" else None)
    monkeypatch.setattr(clarify, "current", lambda: SimpleNamespace(session_key="own-session"))
    token = lark.danger.set_cwd(str(workspace))
    try:
        for root in (workspace, output):
            assert await lark_images.image_snapshot(str(root / "image.png"))
        with pytest.raises(ValueError, match="未读取"):
            await lark_images.image_snapshot(str(other / "image.png"))
    finally:
        lark.danger.reset_cwd(token)


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["partial_success", "failed"])
async def test_partial_resource_write_is_not_reported_as_success(monkeypatch, status):
    monkeypatch.setattr(lark.danger, "require_approval", AsyncMock(return_value=True))
    run = AsyncMock(return_value={"ok": True, "data": {"result": status, "warnings": ["image import failed"]}})
    monkeypatch.setattr(lark, "run_cli", run)
    result = await lark.execute({"operation": "create", "content": "测试"})
    assert result["isError"] and status in result["content"][0]["text"]
    run.assert_awaited_once()
