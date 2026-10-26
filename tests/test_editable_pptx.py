"""真实 PptxGenJS 生成、原生对象/工作簿、输入边界与文件交付闭环。"""
import copy
import importlib.util
import io
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
GENERATE = SKILL / "scripts/generate.cjs"
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
spec = importlib.util.spec_from_file_location("pptx_check", SKILL / "scripts/check.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def content():
    return json.loads((SKILL / "examples/six-slides.json").read_text())


@pytest.fixture
def node():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("PPT 集成测试需要 Node.js")
    cache = Path(os.environ.get("VOCOCO_PPTX_HOME", Path.home() / ".cache/vococo-pptx/4.0.1"))
    if not (cache / "node_modules/pptxgenjs").is_dir():
        pytest.skip("请先运行 editable-pptx/scripts/setup.py，测试不联网安装")
    return executable


def generate(node, tmp_path, content, name="deck"):
    source = tmp_path / f"{name}.json"
    output = tmp_path / f"{name}.pptx"
    source.write_text(json.dumps(content, ensure_ascii=False))
    process = subprocess.run([node, str(GENERATE), str(source), str(output)], capture_output=True, text=True, timeout=45)
    return process, output


def test_six_slides_native_text_table_chart_and_embedded_editable_data(node, tmp_path, content):
    process, output = generate(node, tmp_path, content)
    assert process.returncode == 0, process.stderr
    result = checker.inspect(output)
    assert result["slide_count"] == 6 and not result["errors"]
    assert result["native_charts"] == result["embedded_workbooks"] == 1
    assert result["slides"][3]["tables"] == 1 and result["visual_check"] == "not_run"
    assert sum(slide["images"] for slide in result["slides"]) == 0
    with zipfile.ZipFile(output) as archive:
        slide = ET.fromstring(archive.read("ppt/slides/slide2.xml"))
        assert "先确认受众" in "".join(slide.itertext())
        chart = ET.fromstring(archive.read("ppt/charts/chart1.xml"))
        assert len(chart.findall(".//c:ser", NS)) == 2
        assert [e.text for e in chart.findall(".//c:ser/c:val/c:numRef/c:numCache/c:pt/c:v", NS)][:4] == ["20", "35", "50", "70"]
        book = next(n for n in archive.namelist() if n.endswith(".xlsx"))
        with zipfile.ZipFile(io.BytesIO(archive.read(book))) as workbook:
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            assert "73" in [e.text for e in sheet.findall(".//x:v", NS)]


@pytest.mark.parametrize("kind", ["empty", "too-many", "unknown-layout", "unknown-field", "long-title", "newline",
                                  "long-column", "ragged-table", "big-table", "nan", "mismatched-chart", "negative-pie",
                                  "zero-pie", "remote-image", "escape-image", "secret-image", "bad-color", "null"])
def test_invalid_input_produces_no_pptx(node, tmp_path, content, kind):
    if kind == "empty": content["slides"] = []
    elif kind == "too-many": content["slides"] *= 9
    elif kind == "unknown-layout": content["slides"][0]["layout"] = "html"
    elif kind == "unknown-field": content["slides"][0]["script"] = "execute()"
    elif kind == "long-title": content["slides"][0]["title"] = "长" * 33
    elif kind == "newline": content["slides"][1]["items"] = ["行\n" * 20]
    elif kind == "long-column": content["slides"][2]["columns"][0]["items"] = ["长" * 29]
    elif kind == "ragged-table": content["slides"][3]["rows"][0].pop()
    elif kind == "big-table": content["slides"][3]["rows"] *= 2
    elif kind == "nan": content["slides"][4]["series"][0]["values"][0] = float("inf")
    elif kind == "mismatched-chart": content["slides"][4]["series"][0]["values"].pop()
    elif kind in {"negative-pie", "zero-pie"}:
        content["slides"] = [{"layout": "chart", "title": "错误数据", "chartType": "pie", "labels": ["甲", "乙"],
                              "series": [{"name": "示例", "values": [-1, 2] if kind == "negative-pie" else [0, 0]}]}]
    elif kind.endswith("image"):
        value = {"remote-image": "https://example.com/a.png", "escape-image": "../outside.png", "secret-image": ".env/pic.png"}[kind]
        content["slides"] = [{"layout": "image", "title": "图片", "path": value}]
    elif kind == "bad-color": content["theme"]["accent"] = "red"
    elif kind == "null": content = None
    process, output = generate(node, tmp_path, content)
    assert process.returncode != 0
    assert not output.exists()


def test_existing_output_is_not_overwritten(node, tmp_path, content):
    output = tmp_path / "deck.pptx"
    output.write_bytes(b"keep-existing")
    process, _ = generate(node, tmp_path, content)
    assert process.returncode != 0 and "已存在" in process.stderr
    assert output.read_bytes() == b"keep-existing"


@pytest.mark.parametrize("chart_type", ["line", "pie"])
def test_other_native_chart_types(node, tmp_path, content, chart_type):
    slide = copy.deepcopy(content["slides"][4])
    slide["chartType"] = chart_type
    slide["series"] = slide["series"][:1]
    process, output = generate(node, tmp_path, {"title": "图表", "slides": [slide]})
    assert process.returncode == 0, process.stderr
    assert checker.inspect(output)["native_charts"] == 1


def test_local_image_is_embedded_and_symbolic_link_rejected(node, tmp_path):
    from PIL import Image
    file = tmp_path / "pic.png"
    Image.new("RGB", (480, 240), "orange").save(file)
    content = {"title": "图文页", "slides": [{"layout": "image", "title": "可替换的图片", "path": "pic.png", "caption": "图片是图片对象，不是整页截图"}]}
    process, output = generate(node, tmp_path, content)
    assert process.returncode == 0, process.stderr
    result = checker.inspect(output)
    assert result["slides"][0]["images"] == 1 and not result["errors"]
    (tmp_path / "link.png").symlink_to(file)
    content["slides"][0]["path"] = "link.png"
    process, output = generate(node, tmp_path, content, "linked")
    assert process.returncode != 0 and not output.exists()


def test_missing_office_is_explicit_and_does_not_fake_visual_success(monkeypatch, tmp_path):
    monkeypatch.setattr(checker.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="未安装 LibreOffice"):
        checker.render(tmp_path / "deck.pptx", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_real_pptx_passes_private_artifact_safety_scan(node, tmp_path, content, monkeypatch, isolated):
    from vococo.tools import artifacts
    process, output = generate(node, tmp_path, content)
    assert process.returncode == 0, process.stderr
    monkeypatch.setattr(artifacts, "_roots", lambda *_: (tmp_path.resolve(),))
    name, data = artifacts._snapshot(str(output), "web:pptx", str(tmp_path))
    assert name == output.name and data == output.read_bytes()


def test_internal_skill_available_even_with_skill_whitelist():
    from vococo.core.agent import _PLUGIN_SKILLS
    assert "vococo-internal:editable-pptx" in _PLUGIN_SKILLS
    text = (SKILL / "SKILL.md").read_text()
    assert "send_file" in text and "未验证实际 Office 排版" in text
