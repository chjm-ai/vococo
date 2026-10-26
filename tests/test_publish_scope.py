"""临时 HTML 发布器不得接管业务仓库的既有部署流程。"""
from pathlib import Path

from vococo.core.business_repositories import RepositoryCopy, prompt_note
from vococo.tools import builtin


SKILL = Path(__file__).parents[1] / "vococo/plugin/skills/vococo-web-publish/SKILL.md"


def test_skill_routes_temporary_html_but_excludes_business_repositories():
    text = SKILL.read_text()
    metadata, body = text[4:].split("\n---\n", 1)
    assert "临时" in metadata and "HTML" in metadata
    assert "报告" in metadata and "PPT" in metadata
    assert "业务仓库的网站/应用上线不适用" in metadata
    assert "文件在仓库内也一样" in metadata
    assert "不按文件是否位于仓库判断" in body
    assert "deploy.sh" in body and "不受本技能限制" in body
    assert "不能因为本工具不支持" in body
    assert "审批" in body and "远端" in body


def test_tool_description_limits_unified_publish_rule_to_temporary_artifacts():
    description = builtin.publish_website.description
    assert "临时 HTML" in description
    assert "业务仓库" in description and "部署脚本" in description
    assert "不受本工具限制" in description


def test_bound_repository_prompt_explicitly_preserves_native_deployment():
    copy = RepositoryCopy(Path("/source"), Path("/copy"), "vococo/test")
    note = prompt_note((copy,))
    assert "部署脚本" in note and "publish_website" in note
    assert "不适用" in note and "审批" in note
    assert "仓库内的临时 HTML" in note and "仍走 publish_website" in note
