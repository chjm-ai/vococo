"""Office 只读预览：本地解析、限制解压体积和输出，不执行公式、宏或外部资源。"""
from __future__ import annotations

import base64
import csv
import io
from itertools import islice
from pathlib import Path
from zipfile import ZipFile

MAX_BYTES = 10 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_ROWS = 500
MAX_COLS = 50
MAX_SHEETS = 10
MAX_BLOCKS = 500
EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv", ".docx"}
LIMIT_NOTE = "只读预览；公式显示文件中已保存的结果，不重新计算。最多 10 个工作表，每表最多 500 行、50 列、10 万字，单元格最多 1000 字；不展示图表及原版样式。"


def _text(value) -> str:
    return "" if value is None else str(value)[:1000]


def _check_archive(content: bytes) -> None:
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError("文件解压后过大，无法安全预览，请下载原件。")
        # 拒绝实体声明，避免解析库差异导致 XML 实体展开；不解压到磁盘。
        for entry in entries:
            if entry.filename.endswith((".xml", ".rels")):
                data = archive.read(entry)
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise ValueError("文件包含不支持的 XML 声明。")


def _sheet(name, rows, truncated=False) -> dict:
    values, remaining = [], 100_000
    for row in rows[:MAX_ROWS]:
        cells = list(map(_text, row[:MAX_COLS]))
        remaining -= sum(map(len, cells))
        if remaining < 0:
            truncated = True
            break
        values.append(cells)
    return {"name": name, "rows": values,
            "truncated": truncated or len(rows) > MAX_ROWS}


def _xlsx(content: bytes) -> dict:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
    try:
        sheets = []
        for sheet in book.worksheets[:MAX_SHEETS]:
            # 某些导出工具写错 dimensions；按实际 XML 读取并用固定上限截断。
            sheet.reset_dimensions()
            rows = list(sheet.iter_rows(max_row=MAX_ROWS + 1, max_col=MAX_COLS + 1, values_only=True))
            while rows and not any(value is not None for value in rows[-1]):
                rows.pop()
            width = max((index + 1 for row in rows for index, value in enumerate(row) if value is not None), default=0)
            rows = [row[:width] for row in rows]
            sheets.append(_sheet(sheet.title, rows, width > MAX_COLS))
        return {"kind": "spreadsheet", "sheets": sheets, "note": LIMIT_NOTE,
                "truncated": len(book.worksheets) > MAX_SHEETS}
    finally:
        book.close()


def _xls(content: bytes) -> dict:
    import xlrd

    book = xlrd.open_workbook(file_contents=content, on_demand=True, ragged_rows=True)
    try:
        sheets = []
        for index in range(min(book.nsheets, MAX_SHEETS)):
            sheet = book.sheet_by_index(index)
            rows = [[_xls_cell(sheet.cell(row, col), book.datemode)
                     for col in range(min(sheet.row_len(row), MAX_COLS))]
                    for row in range(min(sheet.nrows, MAX_ROWS + 1))]
            sheets.append(_sheet(sheet.name, rows, sheet.ncols > MAX_COLS))
            book.unload_sheet(index)
        return {"kind": "spreadsheet", "sheets": sheets, "note": LIMIT_NOTE,
                "truncated": book.nsheets > MAX_SHEETS}
    finally:
        book.release_resources()


def _xls_cell(cell, datemode):
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode).isoformat(sep=" ")
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    return cell.value


def _csv(content: bytes, extension: str) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        text = content.decode("gb18030")
    delimiter = "\t" if extension == ".tsv" else ","
    if extension == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t").delimiter
        except csv.Error:
            pass
    rows = list(islice(csv.reader(io.StringIO(text), delimiter=delimiter), MAX_ROWS + 1))
    return {"kind": "spreadsheet", "sheets": [_sheet("表格", rows, any(len(r) > MAX_COLS for r in rows))],
            "note": "只读预览；最多显示 500 行、50 列，单元格最多显示 1000 字。", "truncated": False}


def _image(part) -> dict | None:
    from PIL import Image

    # 重新编码为位图，SVG、外链和活动内容永不进入浏览器。
    with Image.open(io.BytesIO(part.blob)) as image:
        if image.width * image.height > 12_000_000:
            return None
        image.thumbnail((1200, 1200))
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG")
    if output.tell() > 512 * 1024:
        return None
    return {"type": "image", "src": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()}


def _paragraph_blocks(paragraph, budget: dict) -> list[dict]:
    blocks = [{"type": "paragraph", "text": paragraph.text[:10000]}] if paragraph.text else []
    for drawing in paragraph._p.xpath(".//a:blip"):
        if budget["images"] >= 20 or budget["image_bytes"] >= 3 * 1024 * 1024:
            blocks.append({"type": "paragraph", "text": "[其余图片请下载原件查看]"})
            break
        budget["images"] += 1
        relationship = drawing.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        part = paragraph.part.related_parts.get(relationship)
        try:
            image = _image(part) if part else None
        except Exception:
            image = None
        if image:
            budget["image_bytes"] += len(image["src"])
        blocks.append(image or {"type": "paragraph", "text": "[图片无法预览，请下载原件查看]"})
    return blocks


def _docx_table(table) -> dict:
    from docx.oxml.ns import qn

    rows, truncated = [], False
    # 不调用 row.cells：它会按不可信的 gridSpan 完整展开后才允许切片。
    for row in islice(table._tbl.iterchildren(qn("w:tr")), MAX_ROWS + 1):
        cells = []
        for cell in row.iterchildren(qn("w:tc")):
            text = "\n".join("".join(node.text or "" for node in paragraph.iter(qn("w:t")))
                             for paragraph in cell.iterchildren(qn("w:p")))[:1000]
            span = cell.grid_span
            if span < 1:
                span = 1
            cells.extend([text] * min(span, MAX_COLS + 1 - len(cells)))
            if len(cells) > MAX_COLS:
                truncated = True
                break
        rows.append(cells)
    return {"type": "table", **_sheet("", rows, truncated)}


def _docx(content: bytes) -> dict:
    from docx import Document
    from docx.table import Table

    document = Document(io.BytesIO(content))
    import json

    blocks, text_bytes, truncated = [], 0, False
    budget = {"images": 0, "image_bytes": 0}
    for item in islice(document.iter_inner_content(), MAX_BLOCKS + 1):
        if len(blocks) >= MAX_BLOCKS or text_bytes >= 1_000_000:
            truncated = True
            break
        additions = [_docx_table(item)] if isinstance(item, Table) else _paragraph_blocks(item, budget)
        for block in additions:
            if block["type"] != "image":
                text_bytes += len(json.dumps(block, ensure_ascii=False))
        blocks.extend(additions)
    return {"kind": "document", "blocks": blocks[:MAX_BLOCKS], "truncated": truncated or len(blocks) > MAX_BLOCKS,
            "note": "内容预览，非原版排版；不展示页眉页脚、批注和嵌入对象。最多 500 段、100 万字；表格最多 500 行、50 列，单元格最多 1000 字，正文段落最多 10000 字；正文内嵌图片最多 20 张。"}


def parse_office(filename: str, content: bytes) -> dict:
    extension = Path(filename).suffix.lower()
    if extension not in EXTENSIONS:
        raise ValueError("暂不支持这种文件的内容预览。")
    if len(content) > MAX_BYTES:
        raise ValueError("文件超过 10MB，无法预览，请下载原件。")
    try:
        if extension in {".xlsx", ".docx"}:
            _check_archive(content)
        if extension == ".xlsx":
            return _xlsx(content)
        if extension == ".xls":
            return _xls(content)
        if extension == ".docx":
            return _docx(content)
        return _csv(content, extension)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("文件损坏、加密或格式不匹配，无法预览，请下载原件。") from exc


async def office_response(filename: str, content: bytes):
    import asyncio
    from aiohttp import web

    try:
        result = await asyncio.to_thread(parse_office, filename, content)
    except ValueError:
        # 不把第三方库错误中的本机路径等内部细节送到前端。
        return web.json_response({"error": "无法预览：文件损坏、加密或超过安全限制，请下载原件。"},
                                 status=422, headers={"Cache-Control": "private, no-store"})
    return web.json_response(result, headers={"Cache-Control": "private, no-store"})
