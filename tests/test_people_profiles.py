"""人脉画像自动更新(memory/people_profiles.py):Obsidian 笔记扫描、写文件防重。

真实 LLM 调用不在这测(要 key+花钱),`_chat_json` 统一 mock 掉,全部逻辑层验证:
- _note_date:笔记真实日期解析(frontmatter > 文件名前缀 > mtime)
- _iter_candidate_notes / scan_obsidian_notes:目录扫描、水位增量、待确认标记
- 写文件:frontmatter 格式保持、同日防重、基础关系不被覆盖、wikilink 用真实文件名
- analyze:引用核验(LLM 声称的原文引用必须真的存在,防主题联想式幻觉)
"""
from __future__ import annotations

import pytest

from vococo.memory import people_profiles as pp


# ── 笔记日期解析 ────────────────────────────────────────────────────────────
def test_note_date_prefers_frontmatter(tmp_path):
    p = tmp_path / "260814-随便什么.md"
    p.write_text("---\ncreated: 2025-10-29 14:50:12\n---\n正文", encoding="utf-8")
    assert pp._note_date(p, p.read_text(encoding="utf-8")) == "2025-10-29"


def test_note_date_falls_back_to_filename_yymmdd(tmp_path):
    p = tmp_path / "251029 胜源 关于化工出海.md"
    p.write_text("没有 frontmatter 的正文", encoding="utf-8")
    assert pp._note_date(p, p.read_text(encoding="utf-8")) == "2025-10-29"


def test_note_date_falls_back_to_filename_iso(tmp_path):
    p = tmp_path / "2026-07-30-整合校友资源.md"
    p.write_text("没有 frontmatter 的正文", encoding="utf-8")
    assert pp._note_date(p, p.read_text(encoding="utf-8")) == "2026-07-30"


def test_note_date_falls_back_to_mtime(tmp_path):
    p = tmp_path / "未命名.md"
    p.write_text("没有日期线索", encoding="utf-8")
    # mtime 兜底只要求返回合法的 YYYY-MM-DD,不校验具体值
    d = pp._note_date(p, p.read_text(encoding="utf-8"))
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"


# ── 目录扫描 ────────────────────────────────────────────────────────────────
def test_iter_candidate_notes_walks_configured_folders(tmp_path, monkeypatch):
    (tmp_path / "1.个人/思考/2026").mkdir(parents=True)
    (tmp_path / "1.个人/思考/2026/一篇思考.md").write_text("x", encoding="utf-8")
    (tmp_path / "1.个人/社交/聊天").mkdir(parents=True)
    (tmp_path / "1.个人/社交/聊天/250101 聊天.md").write_text("x", encoding="utf-8")
    (tmp_path / "不在配置里的目录").mkdir()
    (tmp_path / "不在配置里的目录/不该扫到.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(pp.config, "OBSIDIAN_VAULT_DIR", tmp_path)
    monkeypatch.setattr(
        pp.config, "PEOPLE_PROFILES_OBSIDIAN_FOLDERS", ["1.个人/思考", "1.个人/社交/聊天"]
    )
    found = pp._iter_candidate_notes()
    names = {f.name: should for f, should in found}
    assert names == {"一篇思考.md": False, "250101 聊天.md": True}  # 思考不强制有人物,聊天要


# ── scan_obsidian_notes:水位增量 + 待确认标记 ───────────────────────────────
@pytest.mark.anyio
async def test_scan_skips_unchanged_files_via_watermark(tmp_path, monkeypatch):
    """mtime 没变的笔记不重复分析(水位生效)。"""
    note = tmp_path / "1.个人/社交/聊天/250101 老笔记.md"
    note.parent.mkdir(parents=True)
    note.write_text("已经扫过的旧内容", encoding="utf-8")

    monkeypatch.setattr(pp.config, "OBSIDIAN_VAULT_DIR", tmp_path)
    monkeypatch.setattr(pp.config, "PEOPLE_PROFILES_OBSIDIAN_FOLDERS", ["1.个人/社交/聊天"])
    monkeypatch.setattr(pp, "_obsidian_watermark_path", lambda: tmp_path / "wm.json")
    monkeypatch.setattr(pp, "_pending_path", lambda: tmp_path / "pending.json")

    calls = []

    async def fake_process_note(text, note_title, note_date):
        calls.append(note_title)
        return {"status": "skipped", "reason": "未提取到人物"}

    monkeypatch.setattr(pp, "process_note", fake_process_note)

    await pp.scan_obsidian_notes()
    assert calls == ["250101 老笔记"]  # 第一次扫描:处理
    await pp.scan_obsidian_notes()
    assert calls == ["250101 老笔记"]  # 第二次:mtime 没变,水位挡住,不重复处理


@pytest.mark.anyio
async def test_scan_reprocesses_after_mtime_change(tmp_path, monkeypatch):
    """笔记被编辑(mtime 更新)→ 下次扫描重新分析。"""
    note = tmp_path / "1.个人/社交/聊天/250101 笔记.md"
    note.parent.mkdir(parents=True)
    note.write_text("v1", encoding="utf-8")

    monkeypatch.setattr(pp.config, "OBSIDIAN_VAULT_DIR", tmp_path)
    monkeypatch.setattr(pp.config, "PEOPLE_PROFILES_OBSIDIAN_FOLDERS", ["1.个人/社交/聊天"])
    monkeypatch.setattr(pp, "_obsidian_watermark_path", lambda: tmp_path / "wm.json")
    monkeypatch.setattr(pp, "_pending_path", lambda: tmp_path / "pending.json")

    calls = []

    async def fake_process_note(text, note_title, note_date):
        calls.append(text)
        return {"status": "skipped", "reason": "未提取到人物"}

    monkeypatch.setattr(pp, "process_note", fake_process_note)
    await pp.scan_obsidian_notes()
    import time as _t
    _t.sleep(0.01)
    note.write_text("v2 改过的内容", encoding="utf-8")
    await pp.scan_obsidian_notes()
    assert calls == ["v1", "v2 改过的内容"]


@pytest.mark.anyio
async def test_scan_flags_pending_when_chat_note_has_no_people(tmp_path, monkeypatch):
    """聊天类笔记零人物提取 → 记待确认项;思考类笔记零人物不标记。"""
    (tmp_path / "1.个人/社交/聊天").mkdir(parents=True)
    (tmp_path / "1.个人/社交/聊天/250101 聊天.md").write_text("内容", encoding="utf-8")
    (tmp_path / "1.个人/思考").mkdir(parents=True)
    (tmp_path / "1.个人/思考/250102 感想.md").write_text("内容", encoding="utf-8")

    monkeypatch.setattr(pp.config, "OBSIDIAN_VAULT_DIR", tmp_path)
    monkeypatch.setattr(
        pp.config, "PEOPLE_PROFILES_OBSIDIAN_FOLDERS", ["1.个人/社交/聊天", "1.个人/思考"]
    )
    monkeypatch.setattr(pp, "_obsidian_watermark_path", lambda: tmp_path / "wm.json")
    monkeypatch.setattr(pp, "_pending_path", lambda: tmp_path / "pending.json")

    async def fake_process_note(text, note_title, note_date):
        return {"status": "skipped", "reason": "未提取到人物"}

    monkeypatch.setattr(pp, "process_note", fake_process_note)
    summaries = await pp.scan_obsidian_notes()
    statuses = {s["note"]: s["status"] for s in summaries}
    assert statuses["1.个人/社交/聊天/250101 聊天.md"] == "pending"
    assert statuses["1.个人/思考/250102 感想.md"] == "skipped"


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
        "张三", {"interaction": "聊了AI项目", "dynamics": ["在做AI产品"], "tags": ["AI"]},
        "260814", "AI项目笔记",
    )
    assert "互动记录+1" in r1
    # 同一天再跑(措辞不同):不追加
    r2 = pp._write_or_update_profile(
        "张三", {"interaction": "讨论AI项目合作", "dynamics": ["在做AI产品中"], "tags": ["AI"]},
        "260814", "另一篇笔记",
    )
    assert "互动记录+1" not in r2
    assert "动态+1条" not in r2
    text = (tmp_path / "张三.md").read_text(encoding="utf-8")
    assert text.count("- 260814 ") == 1          # 互动记录一条
    assert text.count("[260814]") == 1           # 动态一条
    assert "last_updated: 2026-08-14" in text    # 日期刷新,frontmatter 格式不变


@pytest.mark.anyio
async def test_interaction_line_uses_real_note_filename(tmp_path, monkeypatch):
    """互动记录的 wikilink 必须是真实笔记文件名,不是编造的标题——这是这次
    从 vococo turns 数据源改成 Obsidian 笔记数据源的核心修复点。"""
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    pp._write_or_update_profile(
        "张三", {"interaction": "聊了合作"}, "260814", "251029 胜源 关于化工出海和期货交易",
    )
    text = (tmp_path / "张三.md").read_text(encoding="utf-8")
    assert "[[251029 胜源 关于化工出海和期货交易]]" in text


@pytest.mark.anyio
async def test_append_to_last_section_no_trailing_blank_line(tmp_path, monkeypatch):
    """给文件【最后一个】section 追加内容,不能在文件末尾多留一空行。

    真实踩坑:_append_section 不管是不是最后一个 section 都恒定用 "\\n\\n" 分隔
    (为了隔开下一个 "## XX"),但"互动记录"通常是文件最后一节,tail 为空时那个
    "\\n\\n" 就变成了多余的文件末尾空行。
    """
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    (tmp_path / "李四.md").write_text(
        "---\nrelationship: 朋友\ntags: []\nlast_updated: 2026-08-01\n---\n\n"
        "## 互动记录\n- 260801 初次见面 → [[260801-初次见面]]\n",
        encoding="utf-8",
    )
    pp._write_or_update_profile("李四", {"interaction": "聊天"}, "260814", "x")
    text = (tmp_path / "李四.md").read_text(encoding="utf-8")
    assert not text.endswith("\n\n")   # 文件末尾只有一个换行,不是两个
    assert text.endswith("[[x]]\n")


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
        "张三", {"interaction": "一起办活动", "relationship": "工作坊合作伙伴", "tags": []},
        "260814", "活动笔记",
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
    assert text.index("| 新人 |") > text.index("| Bob |")   # 新人排在 Bob 之后
    assert text.index("| 新人 |") > text.index("|---|")     # 且在分隔线之后


@pytest.mark.anyio
async def test_new_profile_file_keeps_template(tmp_path, monkeypatch):
    """新人物:按现有模板建文件,frontmatter 三键齐全,wikilink 用真实笔记名。"""
    monkeypatch.setattr(pp, "people_dir", lambda: tmp_path)
    r = pp._write_or_update_profile(
        "李四", {"interaction": "初识,聊了电商", "dynamics": ["在做出海电商"],
                "occupation": "跨境电商运营", "tags": ["电商出海"], "relationship": "朋友"},
        "260814", "260814-初识李四",
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

    真实踩坑:一段只讲"修bug+看耳鼻喉科"的文本,没提任何人名,LLM 却因为
    "医生/耳鼻喉科"跟已知人物 Alex 的"医疗管理系统"标签主题相似,幻觉出一条
    Alex 互动记录。evidence 字段要求 LLM 提供可核验的原文引用,这里验证:
    编造的引用(原文里真的没有)会被过滤掉,不会进入最终结果。
    """
    text = "刚派了个后台任务修bug,另外最近在看耳鼻喉科,打鼾有点严重。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "Alex", "known": True, "evidence": "跟Alex约了看医生",
             "interaction": "聊了医疗话题", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "笔记标题", "2026-08-14")
    assert result["people"] == []  # 编造的引用在原文里找不到,被丢弃


@pytest.mark.anyio
async def test_analyze_keeps_person_with_real_evidence(monkeypatch):
    """evidence 是原文里真实存在的片段 → 保留。"""
    text = "那这个也是我跟思源还有小玉我们第一次举办AI的线下活动。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "胜源", "known": True, "evidence": "我跟思源还有小玉我们第一次举办AI的线下活动",
             "interaction": "共同举办AI编程工作坊", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "工作坊笔记", "2026-07-31")
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "胜源"


@pytest.mark.anyio
async def test_analyze_keeps_person_named_in_title_without_body_evidence(monkeypatch):
    """名字出现在笔记标题里 → 即使 evidence 留空/正文没有字面提及也保留。

    真实踩坑:"251029 胜源 关于化工出海和期货交易.md" 这类聊天记录,标题写明
    对话对象是胜源,但正文(AI 生成的第三人称总结)全程用"发言人1/发言人2"
    匿名指代,胜源这个名字实际上不会在正文里出现——按纯 evidence-in-body 的
    规则会把标题都点名了的人漏掉,必须给"标题点名"开一条独立的核验路径。
    """
    text = "谈话核心围绕两大主题展开:发言人1和发言人2讨论了化工出海合作。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "胜源", "known": True, "evidence": "",
             "interaction": "深入讨论化工出海和期货交易", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "251029 胜源 关于化工出海和期货交易", "2025-10-29")
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "胜源"


@pytest.mark.anyio
async def test_analyze_drops_person_not_in_title_and_no_evidence(monkeypatch):
    """名字既不在标题里、evidence 也验证不通过 → 仍然丢弃(标题例外不能被滥用)。"""
    text = "谈话核心围绕两大主题展开:发言人1和发言人2讨论了化工出海合作。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "张三", "known": False, "evidence": "",
             "interaction": "编造的互动", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "251029 胜源 关于化工出海和期货交易", "2025-10-29")
    assert result["people"] == []


@pytest.mark.anyio
async def test_analyze_evidence_matches_despite_markdown_and_spacing_noise(monkeypatch):
    """LLM"逐字复制"引用时常无意识去掉 markdown 粗体符号、CJK/数字间距——
    这只是格式噪音,内容一致就该核验通过,不能整批当成编造丢弃。

    真实踩坑:生产全量回扫时,原文"华工校友郑素典专注 PP（聚丙烯）现货交易，
    年营收 70 多亿"(带空格)对应 LLM 引用"华工校友郑素典专注PP（聚丙烯）现货
    交易，年营收70多亿"(无空格)、原文"**关于 Andy：** 他在宝洁做过营销"对应
    LLM 引用"关于 Andy：他在宝洁做过营销"(无 ** 号)——十几条真实信息因为
    字节级 in 判断被误杀,必须做 markdown+空白规整化后再比较。
    """
    text = "同学业务：华工校友郑素典专注 PP（聚丙烯）现货交易，年营收 70 多亿。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "郑素典", "known": False,
             "evidence": "华工校友郑素典专注PP（聚丙烯）现货交易，年营收70多亿",
             "interaction": "专注PP现货交易，年营收70多亿", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "沟通记录整理", "2026-06-15")
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "郑素典"


@pytest.mark.anyio
async def test_analyze_evidence_matches_despite_markdown_bold_marker(monkeypatch):
    """LLM 引用时去掉了原文的 markdown 粗体 ** 记号,内容仍应核验通过。"""
    text = "* **关于 Andy：** 他在宝洁做过营销，现在深耕东南亚跨境电商。"

    async def fake_chat_json(messages):
        return {"people": [
            {"name": "Andy", "known": True,
             "evidence": "关于 Andy：他在宝洁做过营销，现在深耕东南亚跨境电商",
             "interaction": "在宝洁做过营销，现深耕东南亚跨境电商", "dynamics": [], "tags": []},
        ]}

    monkeypatch.setattr(pp, "_chat_json", fake_chat_json)
    result = await pp.analyze(text, "圣诞团契之行", "2025-12-19")
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "Andy"


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


# ── 安全读取:iCloud 驱逐文件不能卡死扫描 ─────────────────────────────────────
@pytest.mark.anyio
async def test_safe_read_text_normal_file(tmp_path):
    p = tmp_path / "正常文件.md"
    p.write_text("正常内容", encoding="utf-8")
    assert await pp._safe_read_text(p) == "正常内容"


@pytest.mark.anyio
async def test_safe_read_text_missing_file_returns_none(tmp_path):
    assert await pp._safe_read_text(tmp_path / "不存在.md") is None


# ── 名字归并:防同音变体重复建人 ──────────────────────────────────────────────
def test_resolve_name_merges_exact_match_with_aliases():
    """精确匹配(含索引别名,如 思源=胜源)直接归并,不发 LLM 请求。"""
    people = [
        {"name": "胜源", "aliases": "盛源, 圣源, 思源, 刘胜源", "relationship": "", "tags": ""},
        {"name": "小玉", "aliases": "", "relationship": "", "tags": ""},
    ]
    # 全走精确查表:没未知名字 → 不调 _chat_json
    import anyio

    async def run():
        return await pp._resolve_name_merges(["思源", "胜源"], people)

    result = anyio.run(run)
    assert result == {"思源": "胜源", "胜源": "胜源"}


@pytest.mark.anyio
async def test_resolve_name_merges_llm_confirm_same_person(monkeypatch):
    """未知名字交给 LLM 复核,确认同音/带姓不带姓是同一人则归并。"""
    people = [
        {"name": "胜源", "aliases": "", "relationship": "", "tags": ""},
        {"name": "喆铭", "aliases": "", "relationship": "", "tags": ""},
    ]
    calls = {}

    async def fake_chat(messages, **kw):
        calls["n"] = calls.get("n", 0) + 1
        return {"merge": {"盛元": "胜源", "黄喆铭": "喆铭"}}

    monkeypatch.setattr(pp, "_chat_json", fake_chat)
    result = await pp._resolve_name_merges(["盛元", "黄喆铭", "全新人物"], people)
    assert calls.get("n") == 1  # 一次批量调用
    assert result == {"盛元": "胜源", "黄喆铭": "喆铭"}  # 全新人物不进映射


@pytest.mark.anyio
async def test_resolve_name_merges_llm_merge_not_in_known_ignored(monkeypatch):
    """LLM 返回的归并目标不在已知名单里(幻觉)→ 忽略该条。"""
    people = [{"name": "胜源", "aliases": "", "relationship": "", "tags": ""}]

    async def fake_chat(messages, **kw):
        return {"merge": {"盛元": "不存在的名字"}}

    monkeypatch.setattr(pp, "_chat_json", fake_chat)
    result = await pp._resolve_name_merges(["盛元"], people)
    assert result == {}


# ── 非人物条目过滤:称谓/敬称/代号/主人自称不建画像 ─────────────────────────
def test_junk_name_filter():
    """名单外的新名字,指代/称谓/敬称/代号/主人自称 → 过滤,不建画像。"""
    assert pp._is_junk_name("化工校友会副会长")
    assert pp._is_junk_name("字节背景女生")
    assert pp._is_junk_name("服装老板")
    assert pp._is_junk_name("酒店咨询朋友")
    assert pp._is_junk_name("雅恩食品客户")
    assert pp._is_junk_name("大牛")
    assert pp._is_junk_name("Open-CLAW")
    assert pp._is_junk_name("Wesley")
    # 拿不准的放行(宁可人工删,不误杀真人物)
    assert not pp._is_junk_name("张老板")      # 称呼式但有实质信息
    assert not pp._is_junk_name("Traster")     # 英文长名,真人
    assert not pp._is_junk_name("小飞")
    assert not pp._is_junk_name("胜源")
