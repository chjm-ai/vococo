"""检查本技能生成的 PPTX；结构检查与真实 Office 渲染明确分开。"""
from pathlib import Path
import argparse
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

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


def inspect(source):
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("PPTX 超过 64MB。")
    with zipfile.ZipFile(source) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError("PPTX 解压内容超过检查上限。")
        size = ET.fromstring(archive.read("ppt/presentation.xml")).find("p:sldSz", NS)
        width, height = int(size.get("cx")), int(size.get("cy"))
        names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"slide(\d+)\.xml", n)[1]))
        if not names:
            raise ValueError("PPTX 没有幻灯片。")
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


def render(source, destination):
    office = office_command()
    poppler = shutil.which("pdftoppm")
    if not poppler:
        raise ValueError("缺少 pdftoppm，无法生成逐页预览。")
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="vococo-office-") as profile:
        subprocess.run([office, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--convert-to", "pdf",
                        "--outdir", str(destination), str(source)], check=True, capture_output=True, timeout=120)
    pdf = destination / f"{source.stem}.pdf"
    if not pdf.is_file():
        raise ValueError("LibreOffice 未生成 PDF，不能继续验收。")
    subprocess.run([poppler, "-scale-to", "1400", "-png", str(pdf), str(destination / "slide")], check=True, timeout=120)
    return {"pdf": str(pdf), "images": [str(p) for p in sorted(destination.glob("slide-*.png"))],
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
            report.update(render(args.pptx.resolve(), args.render.resolve()))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    except (ValueError, OSError, KeyError, ET.ParseError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
