"""人脉画像自动更新(memory/people_profiles.py):信号判断、文本提取、写文件防重。

真实 LLM 调用不在这测(要 key+花钱),`_chat_json` 统一 mock 掉,全部逻辑层验证:
- has_signal:什么文本值得分析(录音/会议分段/口述互动 vs 闲聊)
- _extract_texts:只取主人输入与录音转写,不扫 AI 回复
- 写文件:frontmatter 格式保持、同日防重、基础关系不被覆盖
- analyze:引用核验(LLM 声称的原文引用必须真的存在,防主题联想式幻觉)
"""
from __future__ import annotations

import json

import pytest

from vococo.memory import people_profiles as pp


def _turn_row(user_text: str = "", audios: list | None = None) -> tuple:
    """造一个 turns 行:(id, ts, session_key, user_text, assistant_text, audios)"""
    return (
        1, 1750000000.0, "test:conv1", user_text, "AI 的回复不该被扫描",
        json.dumps(audios, ensure_ascii=False) if audios else "",
    )


# ── 信号判断 ────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_has_signal_audio_long_text():
    """录音转写:≥200 字即分析(没人名正是要问的场景)。"""
    assert pp.has_signal("长" * 200, "audio")
    assert not pp.has_signal("短录音", "audio")


@pytest.mark.anyio
async def test_has_signal_speaker_segments():
    """[说话人N] 会议分段:无条件值得分析。"""
    assert pp.has_signal("[说话人1] 好的\n[说话人2] 收到", "user")


@pytest.mark.anyio
async def test_has_signal_user_text_name_match_no_length_gate(monkeypatch):
    """含已知人名:不设 300 字门槛,只挡纯点名式极短片段(<15字)。

    真实踩坑:"小玉是我的铁哥们,信任程度非常高…帮我找场地、找合作资源"只有
    195 字,曾被旧的"≥300字"门槛挡在外面,是明确的真实信息漏判——门槛该由
    "有没有实质内容"(交给 LLM 判断)决定,不该由字数硬卡。
    """
    monkeypatch.setattr(pp, "known_people", lambda: [{
        "name": "胜源", "aliases": "", "relationship": "朋友", "tags": ""
    }])
    assert pp.has_signal(
        "胜源是我的铁哥们，信任程度非常高，不用怀疑，最近他帮我找了不少资源。", "user"
    )  # 40字左右,含人名,过去会被挡,现在放行
    assert not pp.has_signal("@胜源", "user")  # 纯点名,无实质内容,<15字挡掉


@pytest.mark.anyio
async def test_has_signal_long_text_without_name_match():
    """无人名但够长(≥300字):仍分析——可能是同音异写的新人物或逐字稿。"""
    assert pp.has_signal("聊" * 300, "user")
    assert not pp.has_signal("聊" * 100, "user")  # 无人名且不够长,跳过


# ── 文本提取 ────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_extract_texts_audio_and_user_only():
    """只取录音转写 + 主人输入(不做长度过滤,过滤是 has_signal 的职责);
    AI 回复(assistant_text)一律不扫。"""
    row = _turn_row(
        user_text="胜源" + "聊" * 300,
        audios=[{"file": "a.xm4a", "text": "通话转写" * 60}],
    )
    texts = pp._extract_texts(row)
    assert len(texts) == 2
    assert texts[0][1] == "audio"
    assert texts[1][1] == "user"
    assert all(src != "assistant" for _, src in texts)


@pytest.mark.anyio
async def test_extract_texts_includes_short_user_text():
    """提取阶段不过滤长度——短文本也要拿出来,交给 has_signal 判断值不值得分析。"""
    row = _turn_row(user_text="早上医生的录音", audios=[])
    texts = pp._extract_texts(row)
    assert texts == [("早上医生的录音", "user")]
    assert not pp.has_signal(texts[0][0], texts[0][1])  # 但 has_signal 会挡掉它


# ── 写文件:防重与格式保持 ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_write_update_same_day_dedup(tmp_path, monkeypatch):
    """同一天同一人物:互动记录/动态只追加一次(防 LLM 措辞抖动重复)。"""
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    (tmp_path / "张三.md").write_text(
        "---\nrelationship: 朋友\ntags: [AI]\nlast_updated: 2026-08-01\n---\n\n"
        "## 基本信息\n- 职业：开发者\n\n## 画像\n- 熟悉 AI\n\n## 互动记录\n"
        "- 260801 初次见面 → [[260801-初次见面]]\n",
        encoding="utf-8",
    )
    r1 = pp._write_or_update_profile(
        "张三", {"interaction": "聊了AI项目", "dynamics": ["在做AI产品"],
                "tags": ["AI"], "note_title": "AI项目"}, "260814",
    )
    assert "互动记录+1" in r1
    # 同一天再跑(措辞不同):不追加
    r2 = pp._write_or_update_profile(
        "张三", {"interaction": "讨论AI项目合作", "dynamics": ["在做AI产品中"],
                "tags": ["AI"], "note_title": "AI项目二"}, "260814",
    )
    assert "互动记录+1" not in r2
    assert "动态+1条" not in r2
    text = (tmp_path / "张三.md").read_text(encoding="utf-8")
    assert text.count("- 260814 ") == 1          # 互动记录一条
    assert text.count("[260814]") == 1           # 动态一条
    assert "last_updated: 2026-08-14" in text    # 日期刷新,frontmatter 格式不变


@pytest.mark.anyio
async def test_write_update_base_relationship_not_overridden(tmp_path, monkeypatch):
    """基础关系(朋友)不被一次互动推断的角色覆盖,新角色并入 tags。"""
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    (tmp_path / "张三.md").write_text(
        "---\nrelationship: 朋友\ntags: []\nlast_updated: 2026-08-01\n---\n\n"
        "## 画像\n- 熟悉 AI\n\n## 互动记录\n- 260801 初次见面 → [[260801-初次见面]]\n",
        encoding="utf-8",
    )
    pp._write_or_update_profile(
        "张三", {"interaction": "一起办活动", "relationship": "工作坊合作伙伴",
                "tags": [], "note_title": "活动"}, "260814",
    )
    text = (tmp_path / "张三.md").read_text(encoding="utf-8")
    assert "relationship: 朋友" in text              # 关系保留
    assert "工作坊合作伙伴" in text.split("---")[1]  # 新角色进 tags


@pytest.mark.anyio
async def test_register_index_inserts_after_last_row_not_header(tmp_path, monkeypatch):
    """新人物登记要插在表格最后一条数据行之后,不能插到表头正下方。

    真实踩坑:旧实现用 re.search 找"第一个"匹配 `^\\|.+\\|$` 的行,那正好是
    表头本身(不是最后一行),导致新人物全部堆在表头和分隔线之间,把表格
    结构插坏——生产回扫时连续新建 3 个人物,索引整个乱掉。
    """
    monkeypatch.setattr(pp, "index_path", lambda: tmp_path / "people-network.md")
    (tmp_path / "people-network.md").write_text(
        "---\nname: people-network\n---\n\n## 人脉速查\n\n"
        "| 名字 | 别名 | 关系 | 标签 |\n|---|---|---|---|\n"
        "| Alex |  | 朋友 | AI |\n| Bob |  | 同事 | 电商 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pp, "_index_rows", lambda: [["Alex", "", "朋友", "AI"], ["Bob", "", "同事", "电商"]])
    pp._register_index("新人", {"relationship": "朋友", "tags": ["测试"]})
    text = (tmp_path / "people-network.md").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    # 表头 → 分隔线 → Alex → Bob → 新人,顺序不能乱,新人必须排最后
    assert lines[-4:] == [
        "| 名字 | 别名 | 关系 | 标签 |",
        "|---|---|---|---|",
        "| Alex |  | 朋友 | AI |",
        "| Bob |  | 同事 | 电商 |",
    ] or lines[-1] == "| 新人 |  | 朋友 | 测试 |"
    assert text.index("| 新人 |") > text.index("| Bob |")   # 新人排在 Bob 之后
    assert text.index("| 新人 |") > text.index("|---|")     # 且在分隔线之后


@pytest.mark.anyio
async def test_new_profile_file_keeps_template(tmp_path, monkeypatch):
    """新人物:按现有模板建文件,frontmatter 三键齐全。"""
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    r = pp._write_or_update_profile(
        "李四", {"interaction": "初识,聊了电商", "dynamics": ["在做出海电商"],
                "occupation": "跨境电商运营", "tags": ["电商出海"],
                "relationship": "朋友", "note_title": "初识李四"}, "260814",
    )
    assert r.startswith("新建")
    text = (tmp_path / "李四.md").read_text(encoding="utf-8")
    assert text.startswith("---\nrelationship: 朋友\n")
    assert "tags: [电商出海]" in text
    assert "last_updated: " in text
    assert "## 互动记录" in text
    assert "- 260814 初识,聊了电商 → [[260814-初识李四]]" in text


# ── analyze:引用核验(防主题联想式幻觉)────────────────────────────────────────
@pytest.mark.anyio
async def test_analyze_drops_person_with_fabricated_evidence(monkeypatch):
    """LLM 声称的 evidence 在原文里根本找不到 → 整条丢弃。

    真实踩坑:turn 1789 全文只讲"修bug+看耳鼻喉科",没提任何人名,LLM 却因为
    "医生/耳鼻喉科"跟已知人物 Alex 的"医疗管理系统"标签主题相似,幻觉出一条
    Alex 互动记录。evidence 字段要求 LLM 提供可核验的原文引用,这里验证:
    编造的引用(原文里真的没有)会被过滤掉,不会进入最终结果。
    """
    text = "刚派了个后台任务修bug,另外最近在看耳鼻喉科,打鼾有点严重。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "Alex", "known": True, "evidence": "跟Alex约了看医生",
             "interaction": "聊了医疗话题", "dynamics": [], "tags": [], "note_title": "x"},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text)
    assert result["people"] == []  # 编造的引用在原文里找不到,被丢弃


@pytest.mark.anyio
async def test_analyze_keeps_person_with_real_evidence(monkeypatch):
    """evidence 是原文里真实存在的片段 → 保留。"""
    text = "那这个也是我跟思源还有小玉我们第一次举办AI的线下活动。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "胜源", "known": True, "evidence": "我跟思源还有小玉我们第一次举办AI的线下活动",
             "interaction": "共同举办AI编程工作坊", "dynamics": [], "tags": [], "note_title": "工作坊"},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text)
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "胜源"


# ── DeepSeek 供应商接入(取代 SiliconFlow)────────────────────────────────────
@pytest.mark.anyio
async def test_deepseek_api_key_reuses_settings_page_provider(monkeypatch):
    """key 复用设置页 "deepseek" 第三方供应商(与标题总结兜底同一来源),不新增密钥。"""
    monkeypatch.setattr(
        pp.providers, "sidecar_env",
        lambda name: ("deepseek-v4-flash", {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                                             "ANTHROPIC_API_KEY": "sk-test-123"})
        if name == "deepseek" else None,
    )
    assert pp._deepseek_api_key() == "sk-test-123"


@pytest.mark.anyio
async def test_chat_json_skips_when_deepseek_unconfigured(monkeypatch):
    """设置页没配 deepseek 供应商 → 直接跳过分析,不发请求、不报错。"""
    monkeypatch.setattr(pp.providers, "sidecar_env", lambda name: None)
    result = await pp._chat_json([{"role": "user", "content": "x"}])
    assert result is None
