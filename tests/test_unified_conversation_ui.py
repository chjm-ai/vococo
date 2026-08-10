"""统一对话入口的静态契约测试。"""
from pathlib import Path


STATIC_INDEX = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/index.html"
STATIC_STYLES = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/styles.css"
STATIC_SW = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/sw.js"


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


def test_call_view_uses_exclusive_voice_or_text_panels():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    styles = STATIC_STYLES.read_text(encoding="utf-8")

    assert 'id="voiceModeTab"' in html
    assert 'id="textModeTab"' in html
    assert 'setCallInputMode("voice")' in html
    assert 'setCallInputMode("text")' in html
    assert "#callModeTabs" in styles
    assert "#callVoicePanel[hidden],#callTextPanel[hidden]" in styles


def test_service_worker_cache_version_changes_with_shell_contract():
    sw = STATIC_SW.read_text(encoding="utf-8")

    assert 'const SHELL_CACHE = "vococo-shell-v2";' in sw
