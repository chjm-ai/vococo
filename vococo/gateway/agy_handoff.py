"""本机 CLI 交接：准备可检查的文本快照，不读取会话正文或执行 agy。"""

import json
import os
import re
import sys
from pathlib import Path
from tempfile import mkdtemp

_MAX_FILE = 128 * 1024


def _small_text(path: Path):
    if path.is_symlink() or not path.is_file():
        return None
    with path.open('rb') as file:
        data = file.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE or b'\0' in data:
        return None
    return data.decode('utf-8', errors='replace')


def _standalone_skills(names):
    from . import settings_store

    root = settings_store._SKILLS_DIR.resolve()
    docs = {}
    for name in names:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
            continue
        folder = root / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        if {p.name for p in folder.iterdir()} != {'SKILL.md'}:
            continue  # 有脚本、附件或其他依赖时不做半套迁移
        text = _small_text(folder / 'SKILL.md')
        if text:
            docs[f'.agents/skills/{name}/SKILL.md'] = text
    return docs


def _context(key):
    from .. import config
    from ..memory import agents, session_store
    from . import settings_store

    owner = agents.agent_for_session(key) or agents.get(agents.GENERAL_ID)
    agent_id = owner['id']
    if not re.fullmatch(r'general|[a-f0-9]{6,32}', agent_id):
        raise ValueError('无效的 Agent ID')
    source = Path(owner['workdir'] or config.ROOT_DIR).resolve(strict=True)
    runtime = agents.runtime_for_session(key)
    skill_names = runtime['skills']
    if skill_names is None:
        skill_names = settings_store.effective_skills(str(source), is_explicit_project=bool(owner['workdir']))
    if not isinstance(skill_names, list):
        skill_names = [item['name'] for item in settings_store.list_skills() if item['enabled']]
    mcp_names = set(runtime['mcp'] or ()) | set(session_store.get_external_mcp_names(key))
    mcp_names = sorted(settings_store.effective_external_mcp(mcp_names))
    docs = _standalone_skills(skill_names)
    shared = sorted(Path(name).parent.name for name in docs)
    meta = dict(global_agent_id=agents.GENERAL_ID, agent_id=agent_id, source_project=str(source),
                native_skills=shared, unmapped_skills=sorted(set(skill_names) - set(shared)),
                unmapped_vococo_mcp=mcp_names, local_mcp=['vococo-context'])
    return owner, source, docs, meta


def _read_scope(source, agent_id):
    from ..memory import agents

    docs = {}
    project_rule = _small_text(source / 'AGENTS.md') or _small_text(source / 'CLAUDE.md')
    if project_rule:
        docs['context/PROJECT-RULES.md'] = project_rule
    readme = _small_text(source / 'README.md')
    if readme:
        docs['context/README.md'] = readme
    for aid, label in ((agents.GENERAL_ID, 'GLOBAL-AGENT'), (agent_id, 'AGENT')):
        if aid == agents.GENERAL_ID and label == 'AGENT':
            continue
        home = agents.home(aid)
        if not home.is_symlink() and (doc := _small_text(home / 'AGENT.md')):
            docs[f'context/{label}.md'] = doc
    return docs


def prepare(key: str):
    from .. import config

    owner, source, docs, meta = _context(key)
    root = config.AGENT_WORKSPACES_DIR.resolve() / 'agy-handoff'
    if (root.is_symlink() or root.is_relative_to(config.ROOT_DIR.resolve()) or root.is_relative_to(source)
            or any(os.path.lexists(path / '.git') for path in (root, *root.parents))):
        raise ValueError('交接目录必须独立于项目和 Git 仓库')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise ValueError('交接目录权限必须仅属于当前用户')
    docs.update(_read_scope(source, owner['id']))
    docs['AGENTS.md'] = ('# vococo 本机交接\n\n先读 `context/agent-context.json` 获取归属和能力。'
                         '如果有 `context/PROJECT-RULES.md`，先读并遵循项目规则。'
                         '按需读 `context/` 下的 Agent 职责和项目说明。这里只是快照，'
                         '不包含原会话、完整仓库、笔记库或凭据。不要按源项目路径继续访问。\n')
    docs['context/agent-context.json'] = json.dumps(meta, ensure_ascii=False, indent=2)
    workspace = Path(mkdtemp(prefix=f"{owner['id']}-", dir=root))
    docs['.agents/mcp_config.json'] = json.dumps({'mcpServers': {'vococo-context': {
        'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--serve', str(workspace)],
    }}})
    for relative, text in docs.items():
        target = workspace / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return {'workspace': str(workspace), **meta}


def _serve(workspace: Path):
    from mcp.server.mcpserver import MCPServer

    path = workspace / 'context/agent-context.json'
    payload = _small_text(path)
    if payload is None:
        raise ValueError('缺少交接快照')
    server = MCPServer('vococo-context')

    @server.tool()
    def read_agent_context() -> str:
        """读取本次交接的 Agent ID 和能力清单，无需路径，无写权限。"""
        return payload

    server.run(transport='stdio')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--serve':
    _serve(Path(sys.argv[2]))
