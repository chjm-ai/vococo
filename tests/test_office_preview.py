"""Office 预览：真实文件解析、会话隔离及浏览器点击链路。"""
import asyncio
import io
from pathlib import Path
from zipfile import ZipFile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from docx import Document
from openpyxl import Workbook
from PIL import Image

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway.office_preview import MAX_BYTES, parse_office
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def workbook_bytes():
    book = Workbook()
    book.active.title = "销售"
    book.active.append(["产品", "金额", "<script>alert(1)</script>"])
    book.active.append(["面料", 123.5, "=1+1"])
    book.create_sheet("库存").append(["剩余", 12])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def document_bytes():
    document = Document()
    document.add_paragraph("报告正文 <img src=x onerror=alert(1)>")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "结果"
    image = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(image, format="PNG")
    image.seek(0)
    document.add_picture(image)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_excel_multiple_sheets_and_formula_cache():
    result = parse_office("销售.xlsx", workbook_bytes())
    assert [sheet["name"] for sheet in result["sheets"]] == ["销售", "库存"]
    assert result["sheets"][0]["rows"][1] == ["面料", "123.5", ""]
    assert "<script>" in result["sheets"][0]["rows"][0][2]


def test_legacy_xls_with_ragged_rows_and_empty_sheet():
    import base64

    content = base64.b64decode(Path(__file__).with_name("office_preview_legacy.b64").read_text())
    result = parse_office("旧表.xls", content)
    assert result["sheets"][0]["rows"] == [["产品"], ["面料", "12.0"]]
    assert result["sheets"][1]["rows"] == []


def rewrite_member(content, member, transform):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(content)) as original, ZipFile(output, "w", compression=8) as result:
        for entry in original.infolist():
            data = original.read(entry)
            result.writestr(entry, transform(data) if entry.filename == member else data)
    return output.getvalue()


def test_xlsx_ignores_incorrect_small_dimensions():
    import re

    content = rewrite_member(workbook_bytes(), "xl/worksheets/sheet1.xml",
                             lambda data: re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', data))
    rows = parse_office("错尺寸.xlsx", content)["sheets"][0]["rows"]
    assert len(rows) == 2
    assert rows[1][:2] == ["面料", "123.5"]


def test_docx_large_grid_span_is_bounded_before_expansion():
    content = rewrite_member(document_bytes(), "word/document.xml", lambda data: data.replace(
        b"<w:tcPr>", b'<w:tcPr><w:gridSpan w:val="1000000000"/>', 1))
    result = parse_office("合并表格.docx", content)
    table = next(block for block in result["blocks"] if block["type"] == "table")
    assert len(table["rows"][0]) == 50
    assert table["truncated"]


def test_docx_text_table_and_safe_image_in_order():
    result = parse_office("报告.docx", document_bytes())
    assert [block["type"] for block in result["blocks"]] == ["paragraph", "table", "image"]
    assert result["blocks"][1]["rows"] == [["项目", "结果"]]
    assert result["blocks"][2]["src"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("filename,data", [
    ("a.csv", '产品,备注\n面料,"跨行\n内容"'.encode("utf-8-sig")),
    ("a.csv", '产品,备注\n面料,"跨行\n内容"'.encode("gb18030")),
    ("a.tsv", "产品\t备注\n面料\t跨行内容".encode()),
])
def test_csv_encoding_quotes_and_tsv(filename, data):
    result = parse_office(filename, data)
    assert result["sheets"][0]["rows"][1][0] == "面料"
    assert len(result["sheets"][0]["rows"][0]) == 2


def test_csv_limits_rows_columns_and_text():
    content = (",".join(["x" * 1100] * 51) + "\n") * 100
    result = parse_office("large.csv", content.encode())
    sheet = result["sheets"][0]
    assert sheet["truncated"]
    assert len(sheet["rows"]) <= 500
    assert len(sheet["rows"][0]) == 50
    assert len(sheet["rows"][0][0]) == 1000


@pytest.mark.parametrize("name,content", [
    ("broken.xlsx", b"not an excel file"), ("encrypted.docx", b"bad"),
    ("old.doc", b"bad"), ("large.csv", b"x" * (MAX_BYTES + 1)),
    ("broken.xls", b"bad"),
], ids=["broken-xlsx", "encrypted-docx", "unsupported-doc", "oversize", "broken-xls"])
def test_rejects_bad_encrypted_unsupported_or_large_files(name, content):
    with pytest.raises(ValueError):
        parse_office(name, content)


def test_rejects_entity_and_zip_expansion():
    for name, data in [("a.xml", b'<!DOCTYPE a [<!ENTITY x "hi">]><a>&x;</a>'),
                       ("huge.xml", b"a" * (33 * 1024 * 1024))]:
        output = io.BytesIO()
        with ZipFile(output, "w", compression=8) as archive:
            archive.writestr(name, data)
        with pytest.raises(ValueError):
            parse_office("unsafe.xlsx", output.getvalue())


@pytest.fixture
def office_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    root = isolated / "office"
    root.mkdir()
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(root))
    (root / "sales.xlsx").write_bytes(workbook_bytes())
    (root / "report.docx").write_bytes(document_bytes())
    (root / "bad.xlsx").write_bytes(b"bad")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/doc/preview", adapter._handle_doc_preview),
                    web.get("/artifacts/{artifact_id}/download", adapter._handle_artifact_download)])
    return app, root


@pytest.mark.anyio
async def test_real_path_preview_and_raw_download(office_app):
    app, root = office_app
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/doc/preview?conv=test&path=sales.xlsx&format=office")
        assert response.status == 200
        assert (await response.json())["kind"] == "spreadsheet"
        assert response.headers["Cache-Control"] == "private, no-store"
        response = await client.get("/doc/preview?conv=test&path=sales.xlsx")
        assert await response.read() == (root / "sales.xlsx").read_bytes()
        response = await client.get("/doc/preview?conv=test&path=bad.xlsx&format=office")
        assert response.status == 422
        response = await client.get("/doc/preview?conv=test&path=missing.xlsx&format=office")
        assert response.status == 404


@pytest.mark.anyio
async def test_artifact_preview_uses_snapshot_and_session_ownership(office_app):
    app, _root = office_app
    key = config.resolve_session_key("web", "test")
    turn_id = session_store.start_turn(key, "附件")
    original = workbook_bytes()
    artifact = session_store.append_artifact(key, turn_id, "销售.xlsx", "销售", original)
    async with TestClient(TestServer(app)) as client:
        url = f'/artifacts/{artifact["id"]}/download?conv=test'
        response = await client.get(url + "&format=office")
        assert response.status == 200
        assert (await response.json())["sheets"][0]["name"] == "销售"
        response = await client.get(url)
        assert await response.read() == original
        response = await client.get(url.replace("conv=test", "conv=other") + "&format=office")
        assert response.status == 404


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
async def test_office_preview_browser(office_app, engine):
    from test_library_integration import _browser_environment

    app, _root = office_app
    static = Path("vococo/gateway/adapters/web_static")
    html = '<div id="wrap"></div><aside id="docPreview" hidden><div id="dpHead">' + ''.join(
        f'<button id="{name}"></button>' for name in
        ["dpCopyBtn", "dpOpenBtn", "dpWinBtn", "dpDlBtn", "dpClose"]) + (
        '</div><span id="dpTitle"></span><span id="dpUrlText"></span><div id="dpBody"></div></aside>')
    async def shell(request):
        return web.Response(text='<meta name="viewport" content="width=device-width"><style>' +
                            (static / "styles.css").read_text() + '</style>' + html, content_type="text/html")
    app.router.add_get("/", shell)
    async with TestServer(app) as server:
        process = await asyncio.create_subprocess_exec(
            "node", "tests/office_preview_browser.cjs", str(server.make_url("/")), engine,
            env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
        if process.returncode == 77:
            pytest.skip(output.decode())
        assert process.returncode == 0, output.decode()
        print(output.decode())
