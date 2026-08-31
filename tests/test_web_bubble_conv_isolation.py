"""消息气泡的会话隔离契约。

2026-08-31 修的一类显示错乱:「已发出的消息跨会话显示 / 上一条已发的消息跑到最下面」。
根因是消息区里存在既没有 data-tid(不参与 renderTurns 的轮次 diff)、也没有会话归属的
"裸节点"(乐观用户气泡、命令回执、流式气泡),再叠加 openConv 里两条自己清空 #wrap 却
没同步 _wrapConv 的分支——renderTurns 会误判「没换过会话」而不清空,两个会话的气泡就混在
一起;裸节点又停在 #wrap 末尾,新渲染的权威轮次插到它们上面,看着就是旧消息沉到了最下面。

行为层面的验证在 playwright 无头浏览器里做过(切会话链路实测);这里锁住几条不该被
无意改回去的静态契约。
"""
from pathlib import Path

STATIC = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_clear_wrap_is_the_only_way_to_empty_message_area():
    """清空 #wrap 必须同时改 _wrapConv,否则 renderTurns 会误判没换会话。"""
    index = _read("index.html")
    app_core = _read("app-core.js")

    assert "function clearWrap(conv){" in index
    # openConv 里那两条曾经漏更新 _wrapConv 的分支已改走 clearWrap
    assert 'clearWrap(conv); $("#empty").style.display="none";' in index
    assert 'clearWrap(conv); $("#convLoading").classList.remove("on");' in index
    # 除了 clearWrap 自己和 renderTurns 里那处(它紧跟着就赋值 _wrapConv),
    # 不该再有别处裸着清空消息区
    for line in (index + "\n" + app_core).splitlines():
        if '#wrap").innerHTML=""' not in line:
            continue
        assert "function clearWrap" in line, f"清空 #wrap 请走 clearWrap(conv):{line.strip()}"


def test_bare_bubbles_carry_conversation_stamp():
    """直接贴进 #wrap 的裸节点都要盖会话戳,renderTurns 才认得出该不该清。"""
    stream = _read("stream.js")

    assert "function tagConvNode(node, conv, eph){" in stream
    assert "function retagConvNodes(from, to){" in stream
    # addBubble / 流式气泡两条入口都盖戳
    assert "tagConvNode(row, null, eph);" in stream
    assert "row.append(bubble); tagConvNode(row, null, true);" in stream


def test_render_turns_cleans_foreign_and_superseded_bare_nodes():
    """没换会话时也要清:①别的会话的残留 ②已被权威历史覆盖的 eph 节点。"""
    index = _read("index.html")

    assert 'if(stamp!==String(conv)){ node.remove(); changed=true; }' in index
    assert 'else if(node.dataset.eph==="1" && !busy){ node.remove(); changed=true; }' in index
    # busy 护栏:正在流式/转写的会话不能被自己的历史重绘顺手抹掉进行中的气泡
    assert 'const busy=(S.stream && String(S.conv)===String(conv)) || S.voiceRec[conv];' in index


def test_pure_frontend_notices_are_not_marked_ephemeral():
    """上传失败/发送失败这类提示不在服务端历史里,清掉就回不来,不能标 eph。"""
    composer = _read("composer.js")

    for line in composer.splitlines():
        if 'addBubble("ai","⚠️' in line or 'addBubble("ai", `⚠️' in line:
            assert ", true)" not in line, f"纯前端提示不该标 eph:{line.strip()}"


def test_local_sent_flag_is_per_conversation():
    """全局单布尔会被别的会话(手机端 / cron / 语音任务)的 user 事件误消费。"""
    app_core = _read("app-core.js")
    composer = _read("composer.js")
    stream = _read("stream.js")

    assert "localSent: {}," in app_core
    assert "S.localSent = true;" not in composer
    assert "S.localSent[sendConv] = true;" in composer
    assert "if(S.localSent[e.conv]){ delete S.localSent[e.conv]; break; }" in stream
    # local- 转正时钥匙要跟着换,否则新会话第一条会重复冒泡
    assert "if(S.localSent[oldConv]){ delete S.localSent[oldConv]; S.localSent[sendConv]=true; }" in composer
