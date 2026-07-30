"""会话库:落库 / 载入 / 水位线开新会话 / 拆词检索 / 清空。"""
from __future__ import annotations


def test_append_and_load_recent(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "第一句", "回一")
    session_store.append("cli", "第二句", "回二")
    history = session_store.load_recent("cli")
    assert [t.user for t in history] == ["第一句", "第二句"]  # 旧→新
    assert history[-1].assistant == "回二"


def test_sessions_isolated_by_key(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "给 cli 的", "回 cli")
    session_store.append("tg:1", "给 tg 的", "回 tg")
    assert len(session_store.load_recent("cli")) == 1
    assert len(session_store.load_recent("tg:1")) == 1


def test_new_session_watermark(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "旧轮内容名古屋", "好")
    session_store.new_session("cli")          # 推水位线:旧轮不再载入
    assert session_store.load_recent("cli") == []
    session_store.append("cli", "新轮", "好")
    assert len(session_store.load_recent("cli")) == 1
    # 旧轮仍留库,可被跨会话检索召回
    assert session_store.search("名古屋")


def test_search_multiterm_ranks_by_hits(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "名古屋出差计划", "好")    # id 小,但含「名古屋」「出差」两词
    session_store.append("cli", "随便聊聊出差", "嗯")        # id 大,只含「出差」一词
    rows = session_store.search("名古屋 出差")
    # 命中关键词多的排前 —— 即使 id 更小,也压过只命中 1 词的更新记录
    assert [r[1] for r in rows] == ["名古屋出差计划", "随便聊聊出差"]


def test_search_miss(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "你好", "你好呀")
    assert session_store.search("不存在的关键词xyz") == []


def test_clear(isolated):
    from claude_hermes.memory import session_store

    session_store.append("cli", "a", "b")
    session_store.clear("cli")
    assert session_store.load_recent("cli") == []


def test_search_sessions_title_first_includes_archived(isolated):
    """标题命中排前、正文其次;归档会话照常返回并带 archived 标记。"""
    from claude_hermes.memory import session_store

    session_store.set_title("web:a", "名古屋出差计划")
    session_store.append("web:a", "订机票", "好的")
    session_store.set_title("web:b", "随便聊聊")
    session_store.append("web:b", "去名古屋玩几天", "推荐三日游")
    session_store.set_conv_archived("web:b", True)
    res = session_store.search_sessions("名古屋")
    assert [r["key"] for r in res] == ["web:a", "web:b"]
    assert res[0]["match"] == "title" and res[1]["match"] == "content"
    assert res[1]["archived"] and "名古屋" in res[1]["snippet"]


def test_search_sessions_excludes_deleted_and_foreign_keys(isolated):
    """删除的会话物理消失搜不到;非侧边栏前缀(tg:/cli)不进结果。"""
    from claude_hermes.memory import session_store

    session_store.set_title("web:x", "要删的名古屋会话")
    session_store.append("web:x", "名古屋", "好")
    session_store.append("tg:1", "名古屋", "好")   # TG 会话不属于 Web 侧边栏
    session_store.delete_session("web:x")
    assert session_store.search_sessions("名古屋") == []


def test_search_sessions_like_escape(isolated):
    """用户输入里的 % _ 按字面匹配,不当 LIKE 通配符。"""
    from claude_hermes.memory import session_store

    session_store.set_title("web:a", "进度 100% 完成")
    session_store.set_title("web:b", "进度还差一点")
    res = session_store.search_sessions("100%")
    assert [r["key"] for r in res] == ["web:a"]


def test_project_upsert_dedup_and_hash(isolated, tmp_path):
    from claude_hermes.memory import session_store

    d = tmp_path / "repo"
    d.mkdir()
    p1 = session_store.upsert_project(str(d))
    p2 = session_store.upsert_project(str(d) + "/")  # 带尾斜杠 → 规范化到同一路径
    assert p1["hash"] == p2["hash"] == session_store.project_hash(str(d))
    assert p1["name"] == "repo"
    assert len(session_store.list_projects()) == 1  # 同文件夹去重
    assert session_store.path_for_hash(p1["hash"]) == p1["path"]


def test_project_soft_remove_and_revive(isolated, tmp_path):
    from claude_hermes.memory import session_store

    d = tmp_path / "repo"
    d.mkdir()
    h = session_store.upsert_project(str(d))["hash"]
    session_store.hide_project(h)
    assert session_store.list_projects() == []              # 列表里没了
    assert session_store.path_for_hash(h) == str(d.resolve())  # 但仍可反查(在跑会话不断链)
    session_store.upsert_project(str(d))                    # 再加回同一文件夹
    assert len(session_store.list_projects()) == 1          # 复活


def test_conv_pin_unpin_in_list(isolated):
    import time
    from claude_hermes.memory import session_store

    session_store.set_title("web:a", "会话 A")
    session_store.set_title("web:b", "会话 B")
    session_store.append("web:b", "hello", "ok")  # b 更早,a 更晚,让 a 排前
    time.sleep(0.05)
    session_store.append("web:a", "hi", "ok")
    convs = session_store.list_sessions("web:")
    assert [c["pinned"] for c in convs] == [False, False]

    session_store.set_conv_pinned("web:a", True)
    convs = session_store.list_sessions("web:")
    keys = [c["key"] for c in convs]
    assert keys == ["web:a", "web:b"]
    assert convs[0]["pinned"] is True
    assert convs[1]["pinned"] is False

    session_store.set_conv_pinned("web:a", False)
    assert all(not c["pinned"] for c in session_store.list_sessions("web:"))


def test_ensure_title_returns_placeholder_once(isolated):
    from claude_hermes.memory import session_store

    # 首次:截 40 字设兜底标题,并把它返回(调用方据此起异步总结)
    long_text = "这是一条很长的用户消息" * 8
    placeholder = session_store.ensure_title("web:t1", long_text)
    assert placeholder == long_text[:40]
    assert session_store.get_title("web:t1") == placeholder
    # 已有标题:不覆盖,返回 None(不再触发总结)
    assert session_store.ensure_title("web:t1", "另一条消息") is None
    assert session_store.get_title("web:t1") == placeholder


def test_title_clean_strips_noise():
    from claude_hermes.core import title

    assert title._clean('「会话标题自动总结方案」。') == "会话标题自动总结方案"
    assert title._clean("  首行标题\n第二行应被丢弃") == "首行标题"
    assert title._clean("") == ""
    assert len(title._clean("长" * 100)) == title.MAX_LEN


def test_set_chosen_model_clears_sdk_session_id_on_change(isolated):
    """切换模型时要把旧的 SDK session id 清掉,避免 resume 旧模型导致限额延续。"""
    from claude_hermes.memory import session_store

    session_store.set_chosen_model("web:test", "claude-sonnet-5")
    session_store.set_sdk_session_id("web:test", "sess-123")
    # 同模型不应当清
    session_store.set_chosen_model("web:test", "claude-sonnet-5")
    assert session_store.get_sdk_session_id("web:test") == "sess-123"
    # 切到别的模型必须清
    session_store.set_chosen_model("web:test", "deepseek-chat")
    assert session_store.get_sdk_session_id("web:test") is None


def test_set_chosen_model_first_time_does_not_fail(isolated):
    """首次为会话设置模型(无旧 chosen_model)也能正常落库。"""
    from claude_hermes.memory import session_store

    session_store.set_chosen_model("web:new", "claude-opus-5")
    assert session_store.get_chosen_model("web:new") == "claude-opus-5"


def test_append_turn_image_persists_to_current_turn(isolated):
    """send_image 工具追加的图片要落进当前轮次,刷新历史后仍能看到(而非只推 SSE 就丢)。"""
    from claude_hermes.memory import session_store

    turn_id = session_store.start_turn("web:1", "帮我画个图")
    session_store.append_turn_image("web:1", "ai_abc123.png")
    session_store.finish_turn(turn_id, "画好了,发给你")

    history = session_store.load_history("web:1")
    assert history[-1]["images"] == ["/image?name=ai_abc123.png"]


def test_append_turn_image_merges_with_user_uploaded_images(isolated):
    """同一轮里既有用户上传图又有 AI 主动发图时,追加不能把用户那张覆盖掉。"""
    from claude_hermes.core.agent import ImageAttachment
    from claude_hermes.memory import session_store

    turn_id = session_store.start_turn("web:1", "这张图片里有什么?")
    session_store.save_turn_images(
        turn_id, [ImageAttachment(data="aGVsbG8=", media_type="image/png")]
    )
    session_store.append_turn_image("web:1", "ai_reply.png")
    session_store.finish_turn(turn_id, "已看图并回发一张")

    history = session_store.load_history("web:1")
    assert history[-1]["images"] == [
        f"/image?name={turn_id}_0.png",
        "/image?name=ai_reply.png",
    ]
