"""统一对话入口的静态契约测试。"""
import re
from pathlib import Path


STATIC_INDEX = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/index.html"
STATIC_STYLES = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/styles.css"
STATIC_WORKBENCH = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/workbench.js"
STATIC_SW = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/sw.js"
STATIC_AGENTS = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/agents.js"
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


def test_switching_views_preserves_composer_draft_and_uploading_attachment():
    html = _shell()

    open_view = html[html.index("window.openCallView = function()") : html.index("window.closeCallView = function()")]
    close_view = html[html.index("window.closeCallView = function()") : html.index("// 任务状态条")]

    assert 'saveComposerState(S.conv);' in open_view
    assert 'restoreComposerState(S.conv);' in open_view
    assert 'saveComposerState(S.conv);' in close_view
    assert 'restoreComposerState(S.conv);' in close_view


def test_switching_main_view_refreshes_draft_project_selector():
    html = _shell()

    call_view = html[html.index("window.openCallView = function()") : html.index("window.closeCallView = function()")]
    close_view = html[html.index("window.closeCallView = function()") : html.index("// 任务状态条")]

    assert 'S.conv = "voice-chat:main";' in call_view
    assert "S.conv = S.callReturnConv;" in close_view
    assert "renderProjSelChip();  // 草稿会话切入主会话时，收起仅草稿可见的项目选择器" in call_view
    assert "renderProjSelChip();  // 返回草稿会话时，恢复项目选择器" in close_view


def test_task_status_map_is_shared_with_sidebar_helpers():
    html = _shell()

    assert html.index("const barTasks = new Map();") < html.index("// ── 通话视图:")
    assert "function scheduleDoneHide(){" in html
    assert "const soonest = [...barTasks.values()]" in html
    assert "window.refreshTaskBar = renderTaskBar;" in html
    assert "window.refreshTaskBar?.()" in html


def test_workbench_cancel_holds_before_hiding():
    js = STATIC_WORKBENCH.read_text(encoding="utf-8")

    assert "const WB_TERMINAL_HOLD = new Map();" in js
    cancel_fn = js[js.index("function workbenchBatchCancel(ids){") : js.index("function workbenchBatchSchedule(ids, schedule){")]
    assert "workbenchSwapTask(id)" in cancel_fn
    assert "workbenchHoldTerminalTask(id)" in cancel_fn


def test_service_worker_cache_version_changes_with_shell_contract():
    sw = STATIC_SW.read_text(encoding="utf-8")

    assert 'const SHELL_CACHE = "vococo-shell-v9";' in sw


def test_startup_worktree_cleanup_is_bounded():
    assert "_GIT_TIMEOUT_SEC = 8" in WORKTREE.read_text(encoding="utf-8")
    assert "await asyncio.wait_for(worktree.prune_orphans(), timeout=15)" in GATEWAY_RUN.read_text(
        encoding="utf-8"
    )


def test_search_opened_archived_conversation_keeps_title_and_menu_state():
    """搜索结果不在当前侧栏筛选内时，仍要给详情标题和菜单提供会话对象。"""
    html = _shell()

    assert "searchConvs: []" in html
    assert "async function openSearchResult(r, q){" in html
    assert "S.searchConvs.push({" in html
    assert "openSearchResult(r, q);" in html
    assert "|| (S.searchConvs||[]).find(x=>x.conv===conv);" in html
    assert "? S.searchConvs" in html
    assert "const activeConv=findConv(S.conv);" in html
    assert "syncMoreHeader();   // 搜索先打开任务、列表后到时,收起普通会话的旧菜单" in html


def test_unarchive_search_result_refreshes_active_sidebar():
    """历史搜索打开的归档会话取消归档后，应重拉当前筛选的侧栏列表。"""
    html = _shell()
    archive_fn = html[html.index("async function toggleArchive(conv){") : html.index("function dirtyBits(")]

    assert "if(!next) await loadConvs();" in archive_fn


def _declarations(styles: str, selector: str) -> str:
    """选择器列表里含 selector 的所有规则,把声明拼到一起(只做包含判断,不管层叠)。"""
    pattern = r"[^{}]*" + re.escape(selector) + r"[^{}]*\{([^}]*)\}"
    return ";".join(m.group(1) for m in re.finditer(pattern, styles))


def test_agent_row_working_is_avatar_bounce_and_unread_is_static_badge():
    """Agent 行(2026-09-30 主人定案):工作中 = 头像弹跳;未读 = 头像右上角红色数字角标。
    未读不能再用动效——动的东西扫一眼分不清哪行有未读。"""
    styles = STATIC_STYLES.read_text(encoding="utf-8")

    live = _declarations(styles, '.projgrp.agrow[data-state="working"] .agav')
    assert "animation:agwork" in live and "infinite" in live
    assert '[data-state="done"]' not in styles and "@keyframes agdone" not in styles

    badge = _declarations(styles, ".projgrp.agrow .agbadge")
    assert "position:absolute" in badge and "background:var(--err)" in badge
    assert "animation" not in badge
    # 角标挂在不参与动效的外层上,否则会跟着头像一起跳
    assert "position:relative" in _declarations(styles, ".projgrp.agrow .agavwrap")

    # 圆点样式只服务 .conv 行——Agent 行本身不渲染圆点
    assert ".projgrp.agrow .livedot" not in styles
    assert ".projgrp.agrow .reviewdot" not in styles


def test_agent_row_state_covers_main_children_and_tasks():
    """工作中/未读都要算上主会话 + 名下子会话(+ 归它的后台任务),同一份清单判断。"""
    js = STATIC_AGENTS.read_text(encoding="utf-8")
    items = js[js.index("function agentTasks(") : js.index("function agentWorking(")]
    assert "a.main_conv" in items and "agentOwnsConv(a, c.conv)" in items
    assert "S.voiceSidebar" in items and "agentTasks(a)" in items
    assert "t.agent_id===a.id" in items   # 后台任务按 agent_id 归属,无主才归总助理

    busy = js[js.index("const agentItemBusy=") : js.index("function agentWorking(")]
    assert "S.live[c.conv]" in busy and '"running"' in busy
    working = js[js.index("function agentWorking(") : js.index("function agentUnread(")]
    assert "agentOwnItems(a)" in working and "agentItemBusy" in working
    assert "!t.pinned" in items   # 置顶语音任务不在展开列表里,也不能算进数字
    unread = js[js.index("function agentUnread(") : js.index("function agentKey(")]
    assert "agentOwnItems(a)" in unread and ".length" in unread
    assert "!agentItemBusy(c)" in unread   # 还在跑的那行显示闪点,不算未读
    assert "pending_review" in unread and "S.pendingReview[c.conv]" in unread
    assert "!c.archived" in unread   # 归档的不算,否则数字对不上列表

    row = js[js.index("function renderAgentGroup(") : js.index("function buildAgentMainRow(")]
    assert 'h.dataset.state="working"' in row
    assert 'el("span","agbadge")' in row and '"9+"' in row
    assert 'el("span","livedot")' not in row and 'el("span","reviewdot")' not in row


def test_agent_expanded_lists_main_conversation_first():
    """展开 Agent 后第一行固定是「主会话」,和子会话同一层(主人反馈:主会话收在父级行里看不出来)。
    点 Agent 行本身照样进主会话;名下只有主会话时不给展开(藏箭头、不渲染主会话行)。"""
    js = STATIC_AGENTS.read_text(encoding="utf-8")
    row = js[js.index("function renderAgentGroup(") : js.index("function buildAgentMainRow(")]
    after_open = row[row.index("if(!open) return;") :]
    assert after_open.index("buildAgentMainRow(a, inCall)") < after_open.index("buildConvRow(")
    assert "h.onclick" in row and "openAgentMain(a)" in row
    assert "open=hasChildren && S.expanded.has(k)" in row
    assert '" agnone"' in row and "if(!hasChildren) return;" in row
    styles = STATIC_STYLES.read_text(encoding="utf-8")
    assert "visibility:hidden" in _declarations(styles, ".projgrp.agrow .pgcaret.agnone")

    main_row = js[js.index("function buildAgentMainRow(") : js.index("function openAgentMain(")]
    assert '"主会话"' in main_row and "openAgentMain(a)" in main_row
    assert "openConvMenu" not in main_row   # 主会话不能归档/删除,不给菜单
    assert 'ic("star")' not in main_row     # 主人定案:和子会话同款,不加星标

    # 默认露 5 行 = 主会话 + 4 个子会话,其余折进「展开更多」
    assert "const AGENT_CONV_SHOW_MAX = 4;" in js
    assert "rows.slice(0, AGENT_CONV_SHOW_MAX)" in row


def test_conversation_find_bar_is_wired():
    """会话内查找:标题栏按钮 + 查找条 + find.js 注册进版本化资源/路由/SW 外壳缓存。"""
    html = _shell()
    assert 'id="convFindBtn"' in html and 'id="findBar"' in html
    assert '<script src="/find.js"></script>' in html
    # 全局搜索正文命中点进来时预填同一个词定位
    assert 'openFind(q, {jump:"last", autoLoad:true})' in html
    web_py = (Path(__file__).parents[1] / "vococo/gateway/adapters/web.py").read_text(encoding="utf-8")
    assert '"find.js"' in web_py and "|find|" in web_py
    sw = STATIC_SW.read_text(encoding="utf-8")
    assert '"/find.js"' in sw
