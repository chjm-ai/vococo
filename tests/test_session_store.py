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
