"""本机 CLI 交接：准备可检查的文本快照，不读取会话正文或执行 agy。"""

import json
import os
import re
import sys
from pathlib import Path
from tempfile import mkdtemp

_MAX_FILE = 128 * 1024


def _small_text(path: Path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open('rb') as file:
            data = file.read(_MAX_FILE + 1)
    except OSError:
        return None
    if len(data) > _MAX_FILE or b'\0' in data:
        return None
    return data.decode('utf-8', errors='replace')


_SKILL_SUFFIXES = {'.md', '.txt', '.py', '.sh', '.zsh', '.js', '.mjs', '.ts', '.html', '.css', '.svg'}
_SKILL_SECRET_VALUE = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_\-]{16,}|AIza[A-Za-z0-9_\-]{30,}|'
    r'gh[pousr]_[A-Za-z0-9]{20,})|\b(?:api[_-]?key|password|access[_-]?token)\b["\']?\s*[:=]\s*'
    r'["\'][A-Za-z0-9_+./=-]{12,}["\']', re.I,
)
_SKILL_SECRET_NAME = re.compile(r'credential|password|secrets?|(?:^|[-_])token(?:[-_.]|$)|private[-_]?key', re.I)


def _portable_skill(folder: Path):
    if folder.is_symlink() or not folder.is_dir():
        return {}
    docs, total = {}, 0
    for directory, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ('.git', '__pycache__')]
        if any(name.startswith('.') or _SKILL_SECRET_NAME.search(name) or (Path(directory) / name).is_symlink()
               for name in dirs):
            return {}
        for name in files:
            if name == '.DS_Store':
                continue
            path = Path(directory) / name
            if (name.startswith('.') or _SKILL_SECRET_NAME.search(name) or path.suffix.lower() not in _SKILL_SUFFIXES
                    or (text := _small_text(path)) is None or _SKILL_SECRET_VALUE.search(text)):
                return {}  # 不把缺依赖或携带配置凭据的 Skill 当成已迁移
            total += len(text.encode('utf-8'))
            if total > 512 * 1024 or len(docs) >= 64:
                return {}
            docs[path.relative_to(folder).as_posix()] = text
    return docs if docs.get('SKILL.md') else {}


def _portable_skills(names):
    from . import settings_store

    root = settings_store._SKILLS_DIR.resolve()
    docs, shared = {}, []
    for name in dict.fromkeys(names):
        if re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) and (files := _portable_skill(root / name)):
            docs.update({f'.agents/skills/{name}/{path}': text for path, text in files.items()})
            shared.append(name)
    return docs, sorted(shared)


def _owned_memory_dir(brain: Path, path: Path) -> Path | None:
    relative = path.absolute().relative_to(brain.absolute()) if path.absolute().is_relative_to(brain.absolute()) else None
    if relative is None or not relative.parts or relative.parts[0] != 'memory' or '..' in relative.parts:
        return None
    # AI_BRAIN 本身可以是 iCloud 软链；其下不允许再跳到另一个 Agent 的目录。
    current = brain.resolve()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None
    return current


def _memory_docs(runtime):
    from .. import config
    from ..core.prompt import filter_memory_sections

    docs = {}
    sections = runtime.get('memory_sections')
    # 总助理没有分区；不将整份全局画像/记忆默认交给另一家服务。
    if sections is None:
        from ..memory.agents import default_memory_sections

        sections = default_memory_sections()
    if sections and (index := _small_text(config.AI_BRAIN_DIR / 'MEMORY.md')):
        scoped = filter_memory_sections(index, sections).split('\n\n(以下分节', 1)[0]
        if (first := re.search(r'(?m)^## ', scoped)):
            docs['context/MEMORY.md'] = scoped[first.start():][:12000]
    if (own := runtime.get('memory_dir')):
        path = _owned_memory_dir(config.AI_BRAIN_DIR, Path(own))
        if path and (text := _small_text(path / 'INDEX.md')):
            docs['context/AGENT-MEMORY.md'] = text[:12000]
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
    docs, shared = _portable_skills(skill_names)
    memory = _memory_docs(runtime)
    docs.update(memory)
    meta = dict(global_agent_id=agents.GENERAL_ID, agent_id=agent_id, source_project=str(source),
                native_skills=shared, unmapped_skills=sorted(set(skill_names) - set(shared)),
                unmapped_vococo_mcp=mcp_names, memory_files=sorted(memory))
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


def _cli_settings(server_name):
    settings = {'allowNonWorkspaceAccess': False}
    path = Path.home() / '.gemini/antigravity-cli/settings.json'
    try:
        if os.path.lexists(path):
            existing = json.loads(_small_text(path) or 'null')
            if not isinstance(existing, dict) or existing.get('permissions'):
                return settings  # 不因会话覆盖而抹掉用户自己配置的拒绝或询问规则
    except (OSError, ValueError):
        return settings
    settings['permissions'] = {'allow': [f'mcp({server_name}/{tool})' for tool in (
        'read_agent_context', 'read_agent_memory',
    )]}
    return settings


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
    workspace = Path(mkdtemp(prefix=f"{owner['id']}-", dir=root))
    server_name = f"vococo-context-{workspace.name.split('-', 1)[1]}"
    meta['local_mcp'] = [server_name]
    docs.update(_read_scope(source, owner['id']))
    docs['AGENTS.md'] = (
        '# vococo 本机交接\n\n'
        f"总助理 ID：`{meta['global_agent_id']}`；当前 Agent ID：`{meta['agent_id']}`。\n"
        '开始任务前读取 `context/agent-context.json` 和现有的 `context/AGENT.md`、'
        '`context/GLOBAL-AGENT.md`、`context/PROJECT-RULES.md`。\n'
        f'涉及历史决策、习惯或经验时，先调用只读 MCP `{server_name}` 的 `read_agent_memory`；'
        '工具不可用就按清单读取本地记忆索引，不去原笔记库。匹配任务时按名称加载 `.agents/skills/` 中的 Skill。\n'
        '这里仅有交接快照，不含原会话、完整仓库或凭据。只改当前工作区；'
        '不把未迁移的 vococo MCP 当成可用，也不自动执行对外或破坏性操作。\n'
    )
    docs['context/agent-context.json'] = json.dumps(meta, ensure_ascii=False, indent=2)
    docs['context/cli-settings.json'] = json.dumps(_cli_settings(server_name))
    docs['.agents/mcp_config.json'] = json.dumps({'mcpServers': {server_name: {
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
    memory = {name: text for name in ('MEMORY.md', 'AGENT-MEMORY.md')
              if (text := _small_text(path.parent / name))}
    server = MCPServer('vococo-context')

    @server.tool()
    def read_agent_context() -> str:
        """读取本次交接的 Agent ID 和能力清单，无需路径，无写权限。"""
        return payload

    @server.tool()
    def read_agent_memory() -> dict[str, str]:
        """只读: 返回本次可用的全局分区与 Agent 记忆索引；不读原笔记库。"""
        return memory

    server.run(transport='stdio')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--serve':
    _serve(Path(sys.argv[2]))
