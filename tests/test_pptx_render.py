"""真实 LibreOffice 渲染链的产物核验；不安装依赖、不访问网络。"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "vococo/plugin/skills/editable-pptx"
spec = importlib.util.spec_from_file_location("pptx_render_check", SKILL / "scripts/check.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDTsAAAAASUVORK5CYII=")


def write_deck(source, order=tuple(range(1, 13)), include_list=True):
    namespace = checker.NS["p"]
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    slide_ids = ''.join(f'<p:sldId id="{256 + number}" r:id="rId{number}"/>' for number in order)
    slide_list = f'<p:sldIdLst>{slide_ids}</p:sldIdLst>' if include_list else ''
    relations = ''.join(f'<Relationship Id="rId{number}" Type="{relationship_ns}/slide" Target="slides/slide{number}.xml"/>' for number in range(1, 13))
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{namespace}" xmlns:r="{relationship_ns}">{slide_list}<p:sldSz cx="100" cy="100"/></p:presentation>')
        archive.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relations}</Relationships>')
        for number in range(1, 13):
            # 第一张包页故意越界，检查报错页码是否跟随逻辑重排。
            shape = '<p:sp><p:spPr><a:xfrm><a:off x="-1" y="0"/><a:ext cx="10" cy="10"/></a:xfrm></p:spPr></p:sp>' if number == 1 else ''
            text = ''.join('<a:t>文字</a:t>' for _ in range(number))
            archive.writestr(f"ppt/slides/slide{number}.xml", f'<p:sld xmlns:p="{namespace}" xmlns:a="{checker.NS["a"]}"><p:cSld><p:spTree>{shape}</p:spTree></p:cSld>{text}</p:sld>')


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "原稿.pptx"
    write_deck(source)
    return source


def test_inspection_follows_presentation_order_and_reports_logical_page(deck):
    write_deck(deck, order=(12, 1, 2))
    report = checker.inspect(deck)
    assert report["slide_count"] == 3
    assert [slide["texts"] for slide in report["slides"]] == [12, 1, 2]
    assert report["errors"] == ["第2页：对象超出幻灯片边界"]


def test_unreferenced_slide_files_do_not_count_or_produce_page_errors(deck):
    write_deck(deck, order=(2, 3))
    report = checker.inspect(deck)
    assert report["slide_count"] == 2
    assert [slide["texts"] for slide in report["slides"]] == [2, 3]
    assert report["errors"] == []


def test_missing_logical_slide_list_is_explicitly_rejected(deck):
    write_deck(deck, include_list=False)
    with pytest.raises(ValueError, match="sldIdLst"):
        checker.inspect(deck)


@pytest.mark.parametrize("order", [(2, 2), (99,), ()])
def test_invalid_logical_slide_references_are_rejected(deck, order):
    write_deck(deck, order=order)
    with pytest.raises(ValueError, match="重复引用|关系缺失或无效|sldIdLst"):
        checker.inspect(deck)


@pytest.fixture
def fake_tools(monkeypatch, deck):
    state = {"pdf_pages": 12, "png_pages": list(range(1, 13)), "pdf": b"%PDF-fake",
             "png": PNG, "fail": None, "calls": []}
    monkeypatch.setattr(checker.shutil, "which", lambda name: name)

    def run(command, **kwargs):
        stage = command[0]
        state["calls"].append(command)
        if state["fail"] == stage:
            raise subprocess.CalledProcessError(1, command)
        if stage == "libreoffice":
            profile = Path(command[1].split("file://", 1)[1])
            config = (profile / "user/registrymodifications.xcu").read_text()
            assert 'MacroSecurityLevel' in config and '<value>3</value>' in config
            assert 'DisableMacrosExecution' in config and '<value>true</value>' in config
            assert Path(command[-1]) != deck and Path(command[-1]).read_bytes() == deck.read_bytes()
            Path(command[-1]).write_bytes("转换进程只能修改副本".encode())
            destination = Path(command[command.index("--outdir") + 1])
            if state["pdf"] is not None:
                (destination / f"{deck.stem}.pdf").write_bytes(state["pdf"])
        elif stage == "pdftoppm":
            for number in state["png_pages"]:
                Path(f"{command[-1]}-{number}.png").write_bytes(state["png"])
        elif stage == "pdfinfo":
            assert kwargs["env"]["LC_ALL"] == "C"
            return subprocess.CompletedProcess(command, 0, stdout=f"Pages: {state['pdf_pages']}\n")
        else:
            pytest.fail(f"意外的外部命令：{command}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(checker.subprocess, "run", run)
    return state


def test_render_has_nonempty_pages_numeric_order_and_untouched_original(deck, fake_tools):
    original = deck.read_bytes()
    report = checker.render(deck, deck.parent / "preview")
    assert report["renderer"] == "LibreOffice"
    assert report["page_count"] == 12
    assert [Path(image).name for image in report["images"]] == [f"slide-{i}.png" for i in range(1, 13)]
    assert all(Path(image).stat().st_size > 0 for image in report["images"])
    assert report["visual_check"] == "rendered_needs_human_or_model_review"
    assert deck.read_bytes() == original
    assert 'ExportHiddenSlides' in fake_tools["calls"][0][fake_tools["calls"][0].index("--convert-to") + 1]


def test_render_counts_only_slides_referenced_by_presentation(deck, fake_tools):
    write_deck(deck, order=(3, 2))
    fake_tools["pdf_pages"] = 2
    fake_tools["png_pages"] = [1, 2]
    report = checker.render(deck, deck.parent / "preview")
    assert report["page_count"] == 2 and len(report["images"]) == 2


@pytest.mark.parametrize("missing", ["libreoffice", "pdftoppm", "pdfinfo"])
def test_missing_dependencies_do_not_create_output(deck, monkeypatch, missing):
    monkeypatch.setattr(checker.shutil, "which", lambda name: None if missing == "libreoffice" or name == missing else name)
    with pytest.raises(ValueError, match="未安装 LibreOffice|缺少 pdftoppm 或 pdfinfo"):
        checker.render(deck, deck.parent / "preview")
    assert not (deck.parent / "preview").exists()


@pytest.mark.parametrize("stage", ["libreoffice", "pdftoppm", "pdfinfo"])
def test_conversion_command_failure_is_not_visual_success(deck, fake_tools, stage):
    fake_tools["fail"] = stage
    with pytest.raises(subprocess.CalledProcessError):
        checker.render(deck, deck.parent / "preview")


def test_conversion_timeout_does_not_claim_visual_success(deck, fake_tools, monkeypatch, capsys):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(checker.subprocess, "run", timeout)
    monkeypatch.setattr("sys.argv", ["check.py", str(deck), "--render", str(deck.parent / "preview")])
    assert checker.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["visual_check"] == "not_run" and "timed out" in report["error"]


@pytest.mark.parametrize("pdf", [None, b""])
def test_missing_or_empty_pdf_is_rejected(deck, fake_tools, pdf):
    fake_tools["pdf"] = pdf
    with pytest.raises(ValueError, match="未生成非空 PDF"):
        checker.render(deck, deck.parent / "preview")
    assert all(command[0] != "pdftoppm" for command in fake_tools["calls"])


@pytest.mark.parametrize("pages", [0, 11, 13, "unknown"])
def test_pdf_page_count_must_match_input(deck, fake_tools, pages):
    fake_tools["pdf_pages"] = pages
    with pytest.raises(ValueError, match="PDF 页数"):
        checker.render(deck, deck.parent / "preview")


@pytest.mark.parametrize("pages", [[], list(range(1, 12)), list(range(1, 14)), [0] + list(range(2, 13))])
def test_png_count_and_sequence_must_match_input(deck, fake_tools, pages):
    fake_tools["png_pages"] = pages
    with pytest.raises(ValueError, match="PNG 页数或页序"):
        checker.render(deck, deck.parent / "preview")


@pytest.mark.parametrize("png", [b"", b"not a png", PNG[:24], PNG[:16] + b"\0" * 8 + PNG[24:]])
def test_empty_or_invalid_png_is_rejected(deck, fake_tools, png):
    fake_tools["png"] = png
    with pytest.raises(ValueError, match="PNG 为空或无效"):
        checker.render(deck, deck.parent / "preview")


@pytest.mark.parametrize("kind", ["directory", "file", "broken-link"])
def test_existing_output_is_never_overwritten(deck, fake_tools, kind):
    destination = deck.parent / "preview"
    if kind == "directory":
        destination.mkdir()
        (destination / "keep").write_text("保留")
    elif kind == "file":
        destination.write_text("保留")
    else:
        destination.symlink_to(deck.parent / "missing")
    with pytest.raises(FileExistsError):
        checker.render(deck, destination)
    assert not fake_tools["calls"]
    if kind == "directory":
        assert (destination / "keep").read_text() == "保留"
    elif kind == "file":
        assert destination.read_text() == "保留"
    else:
        assert destination.is_symlink() and not destination.exists()


def test_external_resource_is_rejected_before_office_opens_it(deck, fake_tools):
    with zipfile.ZipFile(deck, "a") as archive:
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", '<Relationships><Relationship Type="image" TargetMode="External" Target="https://example.com/a.png"/></Relationships>')
    with pytest.raises(ValueError, match="外部资源"):
        checker.render(deck, deck.parent / "preview")
    assert not fake_tools["calls"] and not (deck.parent / "preview").exists()


def test_input_outside_workspace_is_rejected(deck, fake_tools, monkeypatch):
    workspace = deck.parent / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(ValueError, match="当前用户工作区"):
        checker.render(deck, workspace / "preview")
    assert not fake_tools["calls"]


def test_cli_failure_keeps_structure_report_without_visual_success(deck, fake_tools, monkeypatch, capsys):
    fake_tools["png_pages"] = []
    monkeypatch.setattr("sys.argv", ["check.py", str(deck), "--render", str(deck.parent / "preview")])
    assert checker.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["visual_check"] == "not_run"
    assert "PNG 页数" in report["error"]
    assert "renderer" not in report


@pytest.mark.parametrize("reorder_and_drop", [False, True])
def test_real_libreoffice_render_when_dependencies_are_installed(tmp_path, monkeypatch, reorder_and_drop):
    try:
        checker.office_command()
    except ValueError:
        pytest.skip("未安装 LibreOffice；测试不会安装")
    node = shutil.which("node")
    cache = Path(os.environ.get("VOCOCO_PPTX_HOME", Path.home() / ".cache/vococo-pptx/4.0.1"))
    if not all(shutil.which(tool) for tool in ("pdftoppm", "pdfinfo")) or not node or not (cache / "node_modules/pptxgenjs").is_dir():
        pytest.skip("缺少本地生成/渲染依赖；测试不安装、不联网")
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "真实原稿.pptx"
    subprocess.run([node, str(SKILL / "scripts/generate.cjs"), str(SKILL / "examples/six-slides.json"), str(source)],
                   check=True, capture_output=True, timeout=45)
    if reorder_and_drop:
        with zipfile.ZipFile(source) as archive:
            entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
        with zipfile.ZipFile(source, "w") as archive:
            for entry, data in entries:
                if entry.filename == "ppt/presentation.xml":
                    root = ET.fromstring(data)
                    slides = root.find("p:sldIdLst", checker.NS)
                    slides[:] = list(reversed(list(slides)[1:]))
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                archive.writestr(entry, data)
    expected = 5 if reorder_and_drop else 6
    original = source.read_bytes()
    report = checker.render(source, tmp_path / "preview")
    assert report["renderer"] == "LibreOffice" and report["page_count"] == expected
    assert len(report["images"]) == expected and Path(report["pdf"]).stat().st_size > 0
    assert all(Path(image).stat().st_size > 24 for image in report["images"])
    assert original == source.read_bytes()
