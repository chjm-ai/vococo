"""检查本技能生成的 PPTX；结构检查与真实 Office 渲染明确分开。"""
from pathlib import Path
import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import unquote

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}


def check_slide(data, width, height):
    root = ET.fromstring(data)
    errors = []
    # 只检查顶层对象；组内坐标不是幻灯片坐标。
    tree = root.find("p:cSld/p:spTree", NS)
    for shape in tree if tree is not None else []:
        transform = shape.find("a:xfrm", NS) if shape.tag.endswith("graphicFrame") else shape.find("p:spPr/a:xfrm", NS)
        if transform is None and shape.tag.endswith("graphicFrame"):
            transform = shape.find("p:xfrm", NS)
        if transform is None:
            continue
        off, ext = transform.find("a:off", NS), transform.find("a:ext", NS)
        if off is None or ext is None:
            continue
        x, y, w, h = (int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")))
        if min(x, y, w, h) < 0 or x + w > width + 100 or y + h > height + 100:
            errors.append("对象超出幻灯片边界")
    return {"texts": len(root.findall(".//a:t", NS)), "tables": len(root.findall(".//a:tbl", NS)),
            "charts": len(root.findall(".//c:chart", NS)), "images": len(root.findall(".//p:pic", NS)), "errors": errors}


def presentation_slides(archive, presentation):
    slide_ids = presentation.find("p:sldIdLst", NS)
    if slide_ids is None or not len(slide_ids):
        raise ValueError("PPTX 没有逻辑幻灯片列表 sldIdLst。")
    relations = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
    relationships = {relation.get("Id"): relation for relation in relations}
    names = []
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    for slide in slide_ids.findall("p:sldId", NS):
        relation = relationships.get(slide.get(f"{{{relationship_ns}}}id"))
        if (relation is None or relation.get("Type") != f"{relationship_ns}/slide"
                or relation.get("TargetMode") == "External" or not relation.get("Target")):
            raise ValueError("PPTX 幻灯片关系缺失或无效。")
        target = unquote(relation.get("Target"))
        name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("ppt", target))
        if name.startswith("../") or name not in archive.namelist():
            raise ValueError("PPTX 引用了不存在的幻灯片。")
        if name in names:
            raise ValueError("PPTX 逻辑页列表重复引用同一幻灯片。")
        names.append(name)
    if not names:
        raise ValueError("PPTX 没有逻辑幻灯片。")
    return names


def inspect(source):
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("PPTX 超过 64MB。")
    with zipfile.ZipFile(source) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError("PPTX 解压内容超过检查上限。")
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = presentation.find("p:sldSz", NS)
        width, height = int(size.get("cx")), int(size.get("cy"))
        names = presentation_slides(archive, presentation)
        slides = [check_slide(archive.read(name), width, height) for name in names]
        charts = [n for n in archive.namelist() if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)]
        workbooks = [n for n in archive.namelist() if n.startswith("ppt/embeddings/") and n.endswith(".xlsx")]
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
    return {"slides": slides, "slide_count": len(slides), "native_charts": len(charts),
            "embedded_workbooks": len(workbooks), "errors": [f"第{i + 1}页：{e}" for i, slide in enumerate(slides) for e in slide["errors"]],
            "visual_check": "not_run", "note": "结构及对象边界通过不代表文字不溢出，仍需逐页渲染检查。"}


def office_command():
    for name in ("libreoffice", "soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        command = shutil.which(name)
        if command:
            return command
    raise ValueError("未安装 LibreOffice，未执行实际 PPT 渲染；不能宣称视觉验收通过。")


def prepare_render_source(source):
    source = source.resolve()
    if not source.is_relative_to(Path.cwd().resolve()):
        raise ValueError("待渲染 PPTX 必须位于当前用户工作区内。")
    count = inspect(source)["slide_count"]
    # 禁止自动读取外链；普通点击超链接不会在转换时更新，可以保留。
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if not name.endswith(".rels"):
                continue
            for relation in ET.fromstring(archive.read(name)):
                if relation.get("TargetMode") == "External" and not relation.get("Type", "").endswith("/hyperlink"):
                    raise ValueError("PPTX 含有外部资源关系，请先嵌入资源再离线渲染。")
    return source, count


def office_profile(profile):
    user = profile / "user"
    user.mkdir(parents=True)
    (user / "registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
  </item>
</oor:items>''', encoding="utf-8")


def rendered_pages(pdf, destination, expected, pdfinfo):
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise ValueError("LibreOffice 未生成非空 PDF，不能继续验收。")
    result = subprocess.run([pdfinfo, str(pdf)], check=True, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "LC_ALL": "C"})
    pages = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not pages or int(pages[1]) != expected:
        raise ValueError(f"PDF 页数与输入 {expected} 页不一致。")
    images = list(destination.glob("slide-*.png"))
    if any(not re.fullmatch(r"slide-\d+\.png", image.name) for image in images):
        raise ValueError("预览 PNG 页码无效。")
    images.sort(key=lambda image: int(image.stem.split("-")[-1]))
    if [int(image.stem.split("-")[-1]) for image in images] != list(range(1, expected + 1)):
        raise ValueError(f"PNG 页数或页序与输入 {expected} 页不一致。")
    for image in images:
        with image.open("rb") as stream:
            header = stream.read(24)
        if (len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR"
                or not int.from_bytes(header[16:20], "big") or not int.from_bytes(header[20:24], "big")
                or image.stat().st_size <= 24):
            raise ValueError(f"预览 PNG 为空或无效：{image.name}")
    return [str(image) for image in images]


def render(source, destination):
    office = office_command()
    poppler, pdfinfo = shutil.which("pdftoppm"), shutil.which("pdfinfo")
    if not poppler or not pdfinfo:
        raise ValueError("缺少 pdftoppm 或 pdfinfo，无法生成并核验逐页预览。")
    source, expected = prepare_render_source(source)
    destination = destination.absolute()
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="vococo-office-") as temporary:
        profile = Path(temporary) / "profile"
        office_profile(profile)
        # 只让 Office 打开副本，原稿不交给转换进程。
        copied = Path(temporary) / source.name
        shutil.copyfile(source, copied)
        export = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'
        subprocess.run([office, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                        "--convert-to", export, "--outdir", str(destination), str(copied)],
                       check=True, capture_output=True, timeout=120)
    pdf = destination / f"{source.stem}.pdf"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise ValueError("LibreOffice 未生成非空 PDF，不能继续验收。")
    subprocess.run([poppler, "-scale-to", "1400", "-png", str(pdf), str(destination / "slide")],
                   check=True, capture_output=True, timeout=120)
    return {"pdf": str(pdf), "images": rendered_pages(pdf, destination, expected, pdfinfo),
            "renderer": "LibreOffice", "page_count": expected,
            "visual_check": "rendered_needs_human_or_model_review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--render", type=Path, help="使用已安装的 LibreOffice 渲染到一个新目录，不覆盖旧预览")
    args = parser.parse_args()
    report = {}
    try:
        report = inspect(args.pptx.resolve())
        if args.render:
            report.update(render(args.pptx.resolve(), args.render.absolute()))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    except (ValueError, OSError, KeyError, ET.ParseError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
