"""统一对话入口的静态契约测试。"""
from pathlib import Path


STATIC_INDEX = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/index.html"
STATIC_STYLES = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/styles.css"
STATIC_SW = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/sw.js"
WORKTREE = Path(__file__).parents[1] / "vococo/core/worktree.py"
GATEWAY_RUN = Path(__file__).parents[1] / "vococo/gateway/run.py"


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
    assert 'mainCt.textContent="主会话"' in html
    assert 'if(!S.voiceSidebarLoaded) return skelRow("voicemain");' not in html
    assert "if(!vs || !vs.main) return null;" not in html
    assert 'onclick="openCallView()">进入主会话</button>' in html
    assert '<span class="title">主会话</span>' in html


def test_call_view_uses_exclusive_voice_or_text_panels():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    styles = STATIC_STYLES.read_text(encoding="utf-8")

    assert 'id="voiceModeTab"' in html
    assert 'id="textModeTab"' in html
    assert 'setCallInputMode("voice")' in html
    assert 'setCallInputMode("text")' in html
    assert 'mountSharedComposer(true);\n    setCallInputMode("text");' in html
    assert html.index('mountSharedComposer(false);') < html.index('if($("#callView").hidden) return;')
    assert "#callModeTabs" in styles
    assert "#callVoicePanel[hidden],#callTextPanel[hidden]" in styles


def test_voice_turns_are_aborted_and_cancelled_when_call_ends():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    assert "let activeVoiceTurn = null;" in html
    assert "function cancelActiveVoiceTurn(reason){" in html
    assert '"X-Voice-Turn-Id": turn.id' in html
    assert "signal: turn.controller.signal" in html
    assert 'cancelActiveVoiceTurn("hangup");' in html
    assert 'cancelActiveVoiceTurn("manual");' in html
    assert "!isActiveVoiceTurn(turn)" in html


def test_closing_call_view_restores_previously_open_conversation():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    assert 'callReturnConv: "main"' in html
    assert 'S.callReturnConv = S.conv || "main";' in html
    assert "S.conv = S.callReturnConv;" in html


def test_switching_main_view_refreshes_draft_project_selector():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    call_view = html[html.index("window.openCallView = function()") : html.index("window.closeCallView = function()")]
    close_view = html[html.index("window.closeCallView = function()") : html.index("// 任务状态条")]

    assert "S.conv = \"voice-chat:main\";\n    renderProjSelChip();" in call_view
    assert "S.conv = S.callReturnConv;\n    renderProjSelChip();" in close_view


def test_task_status_map_is_shared_with_sidebar_helpers():
    html = STATIC_INDEX.read_text(encoding="utf-8")

    assert html.index("const barTasks = new Map();") < html.index("// ── 通话视图:")
    assert "function scheduleDoneHide(){" in html
    assert "const soonest = [...barTasks.values()]" in html
    assert "window.refreshTaskBar = renderTaskBar;" in html
    assert "window.refreshTaskBar?.()" in html


def test_service_worker_cache_version_changes_with_shell_contract():
    sw = STATIC_SW.read_text(encoding="utf-8")

    assert 'const SHELL_CACHE = "vococo-shell-v3";' in sw


def test_startup_worktree_cleanup_is_bounded():
    assert "_GIT_TIMEOUT_SEC = 8" in WORKTREE.read_text(encoding="utf-8")
    assert "await asyncio.wait_for(worktree.prune_orphans(), timeout=15)" in GATEWAY_RUN.read_text(
        encoding="utf-8"
    )
