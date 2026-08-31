"""会话库:落库 / 载入 / 水位线开新会话 / 拆词检索 / 清空。"""
from __future__ import annotations


def test_append_and_load_recent(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "第一句", "回一")
    session_store.append("cli", "第二句", "回二")
    history = session_store.load_recent("cli")
    assert [t.user for t in history] == ["第一句", "第二句"]  # 旧→新
    assert history[-1].assistant == "回二"


def test_recover_interrupted_turns_finishes_pending_rows(isolated):
    from vococo.memory import session_store

    pending = session_store.start_turn("web:pending", "还在吗")
    session_store.flush_draft(pending, "回复到一半")
    finished = session_store.start_turn("web:done", "完成了吗")
    session_store.finish_turn(finished, "已完成")

    recovered = session_store.recover_interrupted_turns()
    assert len(recovered) == 1
    assert recovered[0]["session_key"] == "web:pending"
    assert recovered[0]["user_text"] == "还在吗"
    row = session_store.load_history("web:pending")[-1]
    assert row["pending"] is False
    assert row["assistant"] == "⚠️ 服务重启导致本轮回复中断，请重新发送。"
    assert "draft" not in row
    # 中断标记要写进 events(/history 透传):前端据此识别「被重启打断的回复」,
    # 自动/一键继续生成,而不是让用户手动重发
    assert row["events"] == [{"type": "interrupted"}]


def test_sessions_isolated_by_key(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "给 cli 的", "回 cli")
    session_store.append("task:1", "给 task 的", "回 task")
    assert len(session_store.load_recent("cli")) == 1
    assert len(session_store.load_recent("task:1")) == 1


def test_deleted_turn_cannot_write_to_reused_id(isolated):
    """删除中的旧流拿到已复用的 id，也不能污染后来新建的会话。"""
    from vococo.memory import session_store

    old_key = "web:deleted"
    new_key = "web:current"
    old_id = session_store.start_turn(old_key, "旧会话的问题")
    session_store.delete_session(old_key)
    new_id = session_store.start_turn(new_key, "新会话的问题")
    assert new_id == old_id  # SQLite INTEGER PRIMARY KEY 会复用刚删除的最大 id

    assert not session_store.flush_draft(old_id, "旧会话的残余正文", session_key=old_key)
    assert not session_store.finish_turn(old_id, "旧会话的最终回复", session_key=old_key)

    history = session_store.load_history(new_key)
    assert history[-1]["user"] == "新会话的问题"
    assert history[-1]["pending"] is True
    assert "draft" not in history[-1]


def test_find_session_matches_turn_or_title_visibility(isolated):
    from vococo.memory import session_store

    # 老会话可能只有 turns；新会话在首轮回复前可能只有 title，两种都必须可精确找到。
    session_store.append("web:turn-only", "你好", "你好")
    session_store.set_title("web:title-only", "等首轮回复")
    session_store.set_last_error("web:turn-only", True)

    turn_only = session_store.find_session("web:turn-only")
    assert turn_only == {
        "key": "web:turn-only",
        "title": "新对话",
        "last_error": True,
    }
    assert session_store.find_session("web:title-only")["title"] == "等首轮回复"
    assert session_store.find_session("web:missing") is None


def test_gpt56_summary_normalizes_stale_context_window(isolated):
    """旧会话曾按 API 的 1.05M 落库，Web 进度条必须改按 Codex 的实际有效窗口显示。"""
    from vococo.memory import session_store

    session_store.append("web:gpt", "你好", "你好")
    session_store.set_chosen_model("web:gpt", "gpt-5.6-luna")
    session_store.record_usage("web:gpt", 100_000, 0, window=1_050_000)

    assert session_store.session_summary("web:gpt")["ctx_window"] == 258_400
    assert session_store.list_sessions("web:gpt")[0]["ctx_window"] == 258_400


def test_record_usage_zero_ctx_tokens_keeps_previous_value(isolated):
    """真实 get_context_usage 查询失败时 agent.py 传 0 进来，不能让它把上一轮的
    真实占用冲成 0（2026-08-20 真机事故：查询失败又用不可靠的累计值兜底，
    圆环显示 116%；改成失败就传 0，这里验证 0 不会覆盖旧值）。"""
    from vococo.memory import session_store

    session_store.append("web:c", "你好", "你好")
    session_store.record_usage("web:c", 107_182, 100, window=1_000_000, last_cache=1_163_122)
    assert session_store.session_summary("web:c")["ctx_tokens"] == 107_182

    session_store.record_usage("web:c", 0, 50, window=1_000_000, last_cache=2_000)
    summary = session_store.session_summary("web:c")
    assert summary["ctx_tokens"] == 107_182  # 旧值保留，没被 0 冲掉
    assert summary["last_cache"] == 2_000  # 其它明细仍照常刷新


def test_duplicate_session_copies_turns_and_title(isolated):
    from vococo.memory import session_store

    session_store.append("web:a", "第一句", "回一")
    session_store.append("web:a", "第二句", "回二")
    session_store.set_title("web:a", "原标题")
    session_store.duplicate_session("web:a", "web:b", "原标题副本")
    # 轮次全量复制,顺序不变
    history = session_store.load_recent("web:b")
    assert [t.user for t in history] == ["第一句", "第二句"]
    assert session_store.get_title("web:b") == "原标题副本"
    # 副本是新会话:上下文窗口独立(水位线 0、无 token 计量),不影响原会话
    assert session_store.list_sessions("web:b")[0]["turns"] == 2
    # 副本 ts 刷新 → 排序时顶到原会话前面(侧边栏按 MAX(t.ts) 倒序)
    a = session_store.list_sessions("web:a")[0]
    b = session_store.list_sessions("web:b")[0]
    assert b["last_ts"] >= a["last_ts"]


def test_duplicate_session_empty_source_ok(isolated):
    """复制没有轮次的会话(只有标题)也不报错,新会话带标题。"""
    from vococo.memory import session_store

    session_store.set_title("web:a", "只有标题")
    session_store.duplicate_session("web:a", "web:c", "只有标题副本")
    assert session_store.get_title("web:c") == "只有标题副本"
    assert session_store.load_recent("web:c") == []


def test_new_session_watermark(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "旧轮内容名古屋", "好")
    session_store.new_session("cli")          # 推水位线:旧轮不再载入
    assert session_store.load_recent("cli") == []
    session_store.append("cli", "新轮", "好")
    assert len(session_store.load_recent("cli")) == 1
    # 旧轮仍留库,可被跨会话检索召回
    assert session_store.search("名古屋")


def test_search_multiterm_ranks_by_hits(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "名古屋出差计划", "好")    # id 小,但含「名古屋」「出差」两词
    session_store.append("cli", "随便聊聊出差", "嗯")        # id 大,只含「出差」一词
    rows = session_store.search("名古屋 出差")
    # 命中关键词多的排前 —— 即使 id 更小,也压过只命中 1 词的更新记录
    assert [r[1] for r in rows] == ["名古屋出差计划", "随便聊聊出差"]


def test_search_miss(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "你好", "你好呀")
    assert session_store.search("不存在的关键词xyz") == []


def test_clear(isolated):
    from vococo.memory import session_store

    session_store.append("cli", "a", "b")
    session_store.clear("cli")
    assert session_store.load_recent("cli") == []


def test_search_sessions_title_first_includes_archived(isolated):
    """标题命中排前、正文其次;归档会话照常返回并带 archived 标记。"""
    from vococo.memory import session_store

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
    """删除的会话物理消失搜不到;非侧边栏前缀(cli)不进结果。"""
    from vococo.memory import session_store

    session_store.set_title("web:x", "要删的名古屋会话")
    session_store.append("web:x", "名古屋", "好")
    session_store.append("cli", "名古屋", "好")   # CLI 会话不属于 Web 侧边栏
    session_store.delete_session("web:x")
    assert session_store.search_sessions("名古屋") == []


def test_search_sessions_like_escape(isolated):
    """用户输入里的 % _ 按字面匹配,不当 LIKE 通配符。"""
    from vococo.memory import session_store

    session_store.set_title("web:a", "进度 100% 完成")
    session_store.set_title("web:b", "进度还差一点")
    res = session_store.search_sessions("100%")
    assert [r["key"] for r in res] == ["web:a"]


def test_project_upsert_dedup_and_hash(isolated, tmp_path):
    from vococo.memory import session_store

    d = tmp_path / "repo"
    d.mkdir()
    p1 = session_store.upsert_project(str(d))
    p2 = session_store.upsert_project(str(d) + "/")  # 带尾斜杠 → 规范化到同一路径
    assert p1["hash"] == p2["hash"] == session_store.project_hash(str(d))
    assert p1["name"] == "repo"
    assert len(session_store.list_projects()) == 1  # 同文件夹去重
    assert session_store.path_for_hash(p1["hash"]) == p1["path"]


def test_project_soft_remove_and_revive(isolated, tmp_path):
    from vococo.memory import session_store

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
    from vococo.memory import session_store

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
    from vococo.memory import session_store

    # 首次:截 40 字设兜底标题,并把它返回(调用方据此起异步总结)
    long_text = "这是一条很长的用户消息" * 8
    placeholder = session_store.ensure_title("web:t1", long_text)
    assert placeholder == long_text[:40]
    assert session_store.get_title("web:t1") == placeholder
    # 已有标题:不覆盖,返回 None(不再触发总结)
    assert session_store.ensure_title("web:t1", "另一条消息") is None
    assert session_store.get_title("web:t1") == placeholder


def test_title_clean_strips_noise():
    from vococo.core import title

    assert title._clean('「会话标题自动总结方案」。') == "会话标题自动总结方案"
    assert title._clean("  首行标题\n第二行应被丢弃") == "首行标题"
    assert title._clean("") == ""
    assert len(title._clean("长" * 100)) == title.MAX_LEN


def test_set_chosen_model_clears_sdk_session_id_on_change(isolated):
    """切换模型时要把旧的 SDK session id 清掉,避免 resume 旧模型导致限额延续。"""
    from vococo.memory import session_store

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
    from vococo.memory import session_store

    session_store.set_chosen_model("web:new", "claude-opus-5")
    assert session_store.get_chosen_model("web:new") == "claude-opus-5"


def test_append_turn_image_persists_to_current_turn(isolated):
    """send_image 工具追加的图片要落进当前轮次,刷新历史后仍能看到(而非只推 SSE 就丢)。

    落在 ai_images 而非 images —— images 专属用户上传图(贴回用户气泡),
    ai_images 专属 AI 发的图(贴回 AI 气泡),见 buildTurnBlock。
    """
    from vococo.memory import session_store

    turn_id = session_store.start_turn("web:1", "帮我画个图")
    session_store.append_turn_image("web:1", "ai_abc123.png")
    session_store.finish_turn(turn_id, "画好了,发给你")

    history = session_store.load_history("web:1")
    assert history[-1]["ai_images"] == ["/image?name=ai_abc123.png"]
    assert "images" not in history[-1]


def test_load_recent_restores_user_image_path_for_model(isolated, monkeypatch):
    """历史重建时，模型仍能定位用户此前上传的原图。"""
    from vococo import config
    from vococo.core.agent import ImageAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")
    turn_id = session_store.start_turn("web:1", "把这张图用到网站里")
    image = ImageAttachment(data="aGVsbG8=", media_type="image/png")
    session_store.save_turn_images(turn_id, [image])
    session_store.finish_turn(turn_id, "好的")

    expected = str(config.IMAGES_DIR / f"{turn_id}_0.png")
    history = session_store.load_recent("web:1")
    assert image.local_path == expected
    assert history[-1].image_paths == [expected]


def test_append_turn_image_does_not_touch_user_uploaded_images(isolated):
    """同一轮里既有用户上传图又有 AI 主动发图时,两者要分别落进 images / ai_images,
    不能混在一起(混了会导致 AI 发的图被误贴到用户自己发的那条气泡上)。"""
    from vococo.core.agent import ImageAttachment
    from vococo.memory import session_store

    turn_id = session_store.start_turn("web:1", "这张图片里有什么?")
    session_store.save_turn_images(
        turn_id, [ImageAttachment(data="aGVsbG8=", media_type="image/png")]
    )
    session_store.append_turn_image("web:1", "ai_reply.png")
    session_store.finish_turn(turn_id, "已看图并回发一张")

    history = session_store.load_history("web:1")
    assert history[-1]["images"] == [f"/image?name={turn_id}_0.png"]
    assert history[-1]["ai_images"] == ["/image?name=ai_reply.png"]


def test_thumb_path_generates_and_caches_downscaled_image(isolated, monkeypatch):
    """点开对话详情时聊天气泡只拉缩略图(见 web._handle_image ?thumb=1);首次访问懒生成
    并落盘,二次访问直接命中缓存文件,不重复用 Pillow 压缩。"""
    import io

    from PIL import Image

    from vococo import config
    from vococo.core.agent import ImageAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    buf = io.BytesIO()
    Image.new("RGB", (1000, 800), "red").save(buf, format="PNG")
    b64 = __import__("base64").b64encode(buf.getvalue()).decode()

    turn_id = session_store.start_turn("web:1", "这是一张大图")
    names = session_store.save_turn_images(
        turn_id, [ImageAttachment(data=b64, media_type="image/png")]
    )
    name = names[0]

    thumb = session_store.thumb_path(name)
    assert thumb is not None and thumb.is_file()
    with Image.open(thumb) as im:
        assert max(im.size) <= 320  # 长边压到阈值以内

    cached_thumb = session_store.thumb_path(name)
    assert cached_thumb == thumb  # 二次访问命中缓存,同一份文件


def test_purge_session_images_removes_thumb_too(isolated, monkeypatch):
    """删会话要把缩略图缓存一并清掉,否则孤儿缩略图文件永久堆积在磁盘上。"""
    import io

    from PIL import Image

    from vococo import config
    from vococo.core.agent import ImageAttachment
    from vococo.memory import _db, session_store

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    buf = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(buf, format="PNG")
    b64 = __import__("base64").b64encode(buf.getvalue()).decode()

    turn_id = session_store.start_turn("web:2", "另一张图")
    names = session_store.save_turn_images(
        turn_id, [ImageAttachment(data=b64, media_type="image/png")]
    )
    thumb = session_store.thumb_path(names[0])
    assert thumb.is_file()

    session_store.purge_session_images(_db.conn(), "web:2")
    assert not thumb.is_file()


def test_save_turn_audio_persists_transcript_and_loads_history(isolated, monkeypatch):
    """音频落盘 + 转写文字一起记进 turns.audios;历史里能拿到回放 URL 和转写文字。"""
    from vococo import config
    from vococo.core.agent import AudioAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "data" / "audio")

    turn_id = session_store.start_turn("web:1", "这段录音在说什么?")
    au = AudioAttachment(
        data=b"fake-mp3-bytes", media_type="audio/mpeg",
        filename="memo.mp3", transcript="今天下午三点开会",
    )
    session_store.save_turn_audio(turn_id, [au])
    # 落盘后必须回填本机路径:模型要拿原始音频再处理(转写失败/长音频重跑)全靠它,
    # 没有它就只剩一个磁盘上并不存在的原始文件名,模型只能瞎猜文件在哪
    assert au.local_path == str(config.AUDIO_DIR / f"{turn_id}_0.mp3")
    session_store.finish_turn(turn_id, "这段录音在提醒下午三点开会")

    history = session_store.load_history("web:1")
    # 落盘扩展名以原始文件名为准(memo.mp3 → .mp3),不用 media_type 的子类型 mpeg:
    # 浏览器给 m4a 之类的 type 常为空或不准,落错扩展名会让后续 ffmpeg/ASR 解析失败
    assert history[-1]["audios"] == [
        {
            "url": f"/audio?name={turn_id}_0.mp3",
            "filename": "memo.mp3",
            "text": "今天下午三点开会",
        }
    ]
    assert (config.AUDIO_DIR / f"{turn_id}_0.mp3").read_bytes() == b"fake-mp3-bytes"


def test_save_turn_files_persists_names_and_loads_history(isolated, monkeypatch):
    from vococo import config
    from vococo.core.agent import FileAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "FILES_DIR", isolated / "data" / "files")

    turn_id = session_store.start_turn("web:files", "读取附件")
    att = FileAttachment(data=b"content", media_type="text/markdown", filename="方案.md")
    session_store.save_turn_files(turn_id, [att])
    session_store.finish_turn(turn_id, "已读取")

    # 落盘 + 回填路径:模型要用工具处理原始文件就靠 local_path,只有原始文件名找不到文件。
    # 落盘名用 turn_id 编号 + 原扩展名(原名可能带中文/空格,不进磁盘名)
    saved = config.FILES_DIR / f"{turn_id}_0.md"
    assert saved.read_bytes() == b"content"
    assert att.local_path == str(saved)

    history = session_store.load_history("web:files")
    assert history[-1]["files"] == [
        {"name": "方案.md", "media_type": "text/markdown"}
    ]


def test_clear_purges_attachment_files(isolated, monkeypatch):
    """清空会话要连带把落盘的通用附件删掉,不留孤儿文件(同音频/图片)。"""
    from vococo import config
    from vococo.core.agent import FileAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "FILES_DIR", isolated / "data" / "files")

    turn_id = session_store.start_turn("web:files", "看看这个表")
    session_store.save_turn_files(
        turn_id,
        [FileAttachment(data=b"\x00binary", media_type="application/pdf", filename="报价.pdf")],
    )
    session_store.finish_turn(turn_id, "看完了")
    saved = config.FILES_DIR / f"{turn_id}_0.pdf"
    assert saved.exists()

    session_store.clear("web:files")
    assert not saved.exists()


def test_clear_purges_audio_files(isolated, monkeypatch):
    """清空会话要连带把落盘的音频文件删掉,不留孤儿文件。"""
    from vococo import config
    from vococo.core.agent import AudioAttachment
    from vococo.memory import session_store

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "data" / "audio")

    turn_id = session_store.start_turn("web:1", "听听这个")
    session_store.save_turn_audio(
        turn_id,
        [AudioAttachment(data=b"x", media_type="audio/wav", filename="a.wav", transcript="内容")],
    )
    session_store.finish_turn(turn_id, "好的")
    audio_file = config.AUDIO_DIR / f"{turn_id}_0.wav"
    assert audio_file.exists()

    session_store.clear("web:1")
    assert not audio_file.exists()


def test_load_history_includes_ts(isolated):
    """/turn/regenerate 之类的"重新生成"要在前端就地显示回复时刻,得靠 load_history
    把 turns.ts 带出来(以前这一列存在库里但没进 SELECT)。"""
    from vococo.memory import session_store

    turn_id = session_store.start_turn("web:1", "现在几点")
    session_store.finish_turn(turn_id, "下午三点")

    row = session_store.load_history("web:1")[-1]
    assert isinstance(row["ts"], float) and row["ts"] > 0


def test_load_history_before_id_returns_earlier_turns(isolated):
    """Web 顶部翻页按最早一轮的 id 取更早历史,且结果仍保持旧→新顺序。"""
    from vococo.memory import session_store

    ids = []
    for i in range(5):
        turn_id = session_store.start_turn("web:1", f"问题{i}")
        session_store.finish_turn(turn_id, f"回答{i}")
        ids.append(turn_id)

    page = session_store.load_history("web:1", limit=2, before_id=ids[2])
    assert [row["id"] for row in page] == ids[0:2]
    assert session_store.has_history_before("web:1", ids[3]) is True
    assert session_store.has_history_before("web:1", ids[0]) is False


def test_load_history_before_id_respects_watermark(isolated):
    from vococo.memory import session_store

    for text in ("旧一", "旧二"):
        turn_id = session_store.start_turn("web:1", text)
        session_store.finish_turn(turn_id, "回答")
    session_store.new_session("web:1")
    turn_id = session_store.start_turn("web:1", "新一")
    session_store.finish_turn(turn_id, "回答")

    assert session_store.load_history("web:1", before_id=turn_id) == []
    assert session_store.has_history_before("web:1", turn_id) is False


def test_delete_last_turn_removes_latest_finished_turn(isolated):
    from vococo.memory import session_store

    turn_id = session_store.start_turn("web:1", "重新说一遍")
    session_store.finish_turn(turn_id, "不满意的回答")

    user_text = session_store.delete_last_turn("web:1", turn_id)

    assert user_text == "重新说一遍"
    assert session_store.load_history("web:1") == []


def test_delete_last_turn_rejects_stale_or_pending_turn(isolated):
    """只能删"最新且已完成"那一轮——防止过期按钮误删中间历史,或误删还在
    流式输出中的一轮(assistant_text 还是空串)。"""
    from vococo.memory import session_store

    older = session_store.start_turn("web:1", "第一句")
    session_store.finish_turn(older, "回一")
    newer = session_store.start_turn("web:1", "第二句")
    session_store.finish_turn(newer, "回二")

    # 拿着不是最新一轮的 id 去删:拒绝,数据原样保留
    assert session_store.delete_last_turn("web:1", older) is None
    assert len(session_store.load_history("web:1")) == 2

    # 最新一轮存在但还没写完(pending):同样拒绝
    pending = session_store.start_turn("web:1", "第三句")
    assert session_store.delete_last_turn("web:1", pending) is None
    assert len(session_store.load_history("web:1")) == 3
