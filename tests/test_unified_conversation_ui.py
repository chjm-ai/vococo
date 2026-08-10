"""统一对话入口的静态契约测试。"""
from pathlib import Path


STATIC_INDEX = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/index.html"
STATIC_STYLES = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/styles.css"


def test_call_view_reuses_shared_composer_and_routes_text_to_voice_turn():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    assert "const sharedComposer = $(\"#composer\");" in html
    assert "mountSharedComposer(true);" in html
    assert "window.sendCallText = sendCallText;" in html
    assert "return window.sendCallText(text);" in html
    assert "fetch(\"/voice/send\"" in html


def test_sidebar_has_one_unified_conversation_entry():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    assert 'const mainConv=S.convs.find(c=>c.conv==="main");' not in html
    assert 'mainCt.textContent="对话"' in html
    assert 'onclick="openCallView()">进入对话</button>' in html


def test_call_view_moves_voice_controls_above_shared_composer():
    styles = STATIC_STYLES.read_text(encoding="utf-8")

    assert "#callView.with-composer #callFoot" in styles
    assert "#callView.with-composer #callBody" in styles
