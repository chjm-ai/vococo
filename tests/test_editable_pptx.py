"""移除 PPT 技能不影响现有 PPTX 私有交付。"""
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "vococo/plugin/skills/editable-pptx"


@pytest.fixture
def native_deck(tmp_path):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text = "原生文字"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(2)).table
    table.cell(0, 0).text = "原生表格"
    output = tmp_path / "native.pptx"
    presentation.save(output)
    return output


def test_real_pptx_passes_private_artifact_safety_scan(native_deck, monkeypatch, isolated):
    from vococo.tools import artifacts
    monkeypatch.setattr(artifacts, "_roots", lambda *_: (native_deck.parent.resolve(),))
    name, data = artifacts._snapshot(str(native_deck), "web:pptx", str(native_deck.parent))
    assert name == native_deck.name and data == native_deck.read_bytes()


def test_removed_ppt_skill_is_not_injected_or_discoverable():
    from vococo.core.agent import _PLUGIN_SKILLS
    assert "vococo-internal:editable-pptx" not in _PLUGIN_SKILLS
    assert not (SKILL / "SKILL.md").exists()
    assert "vococo-internal:vococo-web-publish" in _PLUGIN_SKILLS
