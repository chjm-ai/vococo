"""统一对话入口的静态契约测试。"""
from pathlib import Path


STATIC_INDEX = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/index.html"
STATIC_STYLES = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/styles.css"
STATIC_SW = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/sw.js"
WORKTREE = Path(__file__).parents[1] / "vococo/core/worktree.py"
GATEWAY_RUN = Path(__file__).parents[1] / "vococo/gateway/run.py"


def _shell() -> str:
    """2026-08-14 前端模块化后,index.html 只是骨架(状态/顶栏/导航/启动),
    功能代码在按序加载的 7 个 JS 里;契约断言面向「页面实际加载的全部脚本」。"""
    parts = [STATIC_INDEX.read_text(encoding="utf-8")]
    for name in ("app-core", "markdown", "sidebar", "settings", "stream", "composer", "voice"):
        parts.append((STATIC_INDEX.parent / f"{name}.js").read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_call_view_reuses_shared_composer_and_routes_text_to_voice_turn():
    html = _shell()

    assert "const sharedComposer = $(\"#composer\");" in html
    assert "mountSharedComposer(true);" in html
    assert "window.sendCallText = sendCallText;" in html
    assert "return window.sendCallText(text);" in html
    assert "fetch(\"/voice/send\"" in html


def test_sidebar_has_one_unified_conversation_entry():
    html = _shell()

    assert 'const mainConv=S.convs.find(c=>c.conv==="main");' not in html
    assert 'mainCt.textContent="主会话"' in html
    assert 'if(!S.voiceSidebarLoaded) return skelRow("voicemain");' not in html
    assert "if(!vs || !vs.main) return null;" not in html
    assert 'onclick="openCallView()">进入主会话</button>' in html
    assert '<span class="title">主会话</span>' in html


def test_call_view_uses_exclusive_voice_or_text_panels():
    html = _shell()
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
    html = _shell()

    assert "let activeVoiceTurn = null;" in html
    assert "function cancelActiveVoiceTurn(reason){" in html
    assert '"X-Voice-Turn-Id": turn.id' in html
    assert "signal: turn.controller.signal" in html
    assert 'cancelActiveVoiceTurn("hangup");' in html
    assert 'cancelActiveVoiceTurn("manual");' in html
    assert "!isActiveVoiceTurn(turn)" in html


def test_closing_call_view_restores_previously_open_conversation():
    html = _shell()

    assert 'callReturnConv: "main"' in html
    assert 'S.callReturnConv = S.conv || "main";' in html
    assert "S.conv = S.callReturnConv;" in html


def test_switching_main_view_refreshes_draft_project_selector():
    html = _shell()

    call_view = html[html.index("window.openCallView = function()") : html.index("window.closeCallView = function()")]
    close_view = html[html.index("window.closeCallView = function()") : html.index("// 任务状态条")]

    assert "S.conv = \"voice-chat:main\";\n    renderProjSelChip();" in call_view
    assert "S.conv = S.callReturnConv;\n    renderProjSelChip();" in close_view


def test_task_status_map_is_shared_with_sidebar_helpers():
    html = _shell()

    assert html.index("const barTasks = new Map();") < html.index("// ── 通话视图:")
    assert "function scheduleDoneHide(){" in html
    assert "const soonest = [...barTasks.values()]" in html
    assert "window.refreshTaskBar = renderTaskBar;" in html
    assert "window.refreshTaskBar?.()" in html


def test_service_worker_cache_version_changes_with_shell_contract():
    sw = STATIC_SW.read_text(encoding="utf-8")

    assert 'const SHELL_CACHE = "vococo-shell-v8";' in sw


def test_startup_worktree_cleanup_is_bounded():
    assert "_GIT_TIMEOUT_SEC = 8" in WORKTREE.read_text(encoding="utf-8")
    assert "await asyncio.wait_for(worktree.prune_orphans(), timeout=15)" in GATEWAY_RUN.read_text(
        encoding="utf-8"
    )


def test_search_opened_archived_conversation_keeps_title_and_menu_state():
    """搜索结果不在当前侧栏筛选内时，仍要给详情标题和菜单提供会话对象。"""
    html = _shell()

    assert "searchConvs: []" in html
    assert "function openSearchResult(r){" in html
    assert "S.searchConvs.push({" in html
    assert "openSearchResult(r);" in html
    assert "|| (S.searchConvs||[]).find(x=>x.conv===conv);" in html
    assert "? S.searchConvs" in html
    assert "const activeConv=findConv(S.conv);" in html
    assert "syncMoreHeader();   // 搜索先打开任务、列表后到时,收起普通会话的旧菜单" in html


def test_unarchive_search_result_refreshes_active_sidebar():
    """历史搜索打开的归档会话取消归档后，应重拉当前筛选的侧栏列表。"""
    html = _shell()
    archive_fn = html[html.index("async function toggleArchive(conv){") : html.index("function dirtyBits(")]

    assert "if(!next) await loadConvs();" in archive_fn
