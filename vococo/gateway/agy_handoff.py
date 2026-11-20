"""本机 CLI 交接：准备可检查的文本快照，不读取会话正文或执行 agy。"""

import json
import os
import re
import shutil
import sys
from contextlib import suppress
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


def _skill_description(text: str) -> str:
    # 只取 SKILL.md frontmatter/首段的简短说明，不把技能正文塞进常驻上下文。
    front = re.match(r'---\s*\n(.*?)\n---', text, re.S)
    if front:
        found = re.search(r'(?m)^description\s*:\s*(.+?)\s*$', front.group(1))
        if found:
            return found.group(1).strip('"\'')[:200]
    body = re.sub(r'(?m)^---\s*$', '', text)
    for line in body.splitlines():
        line = line.strip().lstrip('#').strip()
        if line:
            return line[:200]
    return ''


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


def _context(key, draft_owner=None):
    from .. import config
    from ..memory import agents, session_store
    from . import settings_store

    # 准备目录与正常发送可能同时发生；已有持久归属优先于草稿参数。
    if draft_owner is not None and session_store.session_binding(key):
        draft_owner = None
    owner = draft_owner or agents.agent_for_session(key) or agents.get(agents.GENERAL_ID)
    agent_id = owner['id']
    if not re.fullmatch(r'general|[a-f0-9]{6,32}', agent_id):
        raise ValueError('无效的 Agent ID')
    source = Path(owner['workdir'] or config.ROOT_DIR).resolve(strict=True)
    runtime = agents.runtime_for_agent(owner) if draft_owner is not None else agents.runtime_for_session(key)
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


_CONTEXT_TOOLS = ('read_agent_context', 'read_agent_memory', 'read_context_file',
                  'list_skills', 'read_skill')
_CONTEXT_FILES = ('agent-context.json', 'MEMORY.md', 'AGENT-MEMORY.md',
                  'PROJECT-RULES.md', 'README.md', 'AGENT.md', 'GLOBAL-AGENT.md')


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
    settings['permissions'] = {'allow': [f'mcp({server_name}/{tool})' for tool in _CONTEXT_TOOLS]}
    return settings


def prepare(key: str, *, draft_owner: dict | None = None):
    from .. import config

    root = config.AGENT_WORKSPACES_DIR.resolve() / 'agy-handoff'
    if (root.is_symlink() or root.is_relative_to(config.ROOT_DIR.resolve())
            or any(os.path.lexists(path / '.git') for path in (root, *root.parents))):
        raise ValueError('交接目录必须独立于项目和 Git 仓库')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise ValueError('交接目录权限必须仅属于当前用户')
    workspace = Path(mkdtemp(prefix='session-', dir=root))
    try:
        meta = prepare_into(workspace, key, draft_owner=draft_owner)
    except Exception:
        # 准备失败时不留下空快照目录；只删刚创建且仍为空的那一层。
        with suppress(OSError):
            workspace.rmdir()
        raise
    return {'workspace': str(workspace), **meta}


def prepare_into(workspace: Path, key: str, *, draft_owner: dict | None = None):
    """把当前 Agent 的规则、技能、记忆索引和只读 MCP 写进既有工作区。

    网页 ACP 会话每轮都调用它，让技能/记忆索引跟随 vococo 最新配置；
    只写 vococo 自己的交接目录，不读会话正文、不改原项目。"""
    owner, source, docs, meta = _context(key, draft_owner)
    workspace = Path(workspace)
    if (workspace.is_symlink() or not workspace.is_dir()
            or workspace.resolve().is_relative_to(Path(source).resolve())
            or any(os.path.lexists(path / '.git') for path in (workspace, *workspace.parents))):
        raise ValueError('交接目录不可用')
    server_name = f"vococo-context-{workspace.name.split('-', 1)[-1]}"
    meta['local_mcp'] = [server_name]
    meta['skills'] = [
        {'name': name, 'description': _skill_description(files['SKILL.md'])}
        for name in meta['native_skills']
        if (files := _portable_skill_from_docs(docs, name))
    ]
    docs.update(_read_scope(source, owner['id']))
    skill_names = '、'.join(item['name'] for item in meta['skills']) or '无'
    docs['AGENTS.md'] = (
        '# vococo 本机交接\n\n'
        f"总助理 ID：`{meta['global_agent_id']}`；当前 Agent ID：`{meta['agent_id']}`。\n"
        '开始任务前用下面的只读 MCP 读取 `agent-context.json`；需要时再读 '
        '`PROJECT-RULES.md`、`AGENT.md`、`GLOBAL-AGENT.md`。\n'
        f'可用 Skill：{skill_names}。任务匹配某个 Skill 时，先调用只读 MCP `{server_name}` 的 '
        '`read_skill` 读取正文再遵循；不要臆测未读取的技能内容。'
        'Skill 文件可能在本轮前被更新，每轮要使用时都要重新读取，不要沿用历史里的技能正文。\n'
        f'项目规则、Agent 职责和项目说明通过只读 MCP `{server_name}` 的 `read_context_file` 读取'
        '（PROJECT-RULES.md / AGENT.md / GLOBAL-AGENT.md / README.md），不要假设自己已经读过。'
        f'涉及历史决策、习惯或经验时，先调用 `read_agent_memory`；'
        f'技能清单可用 `list_skills` 查询，Agent 身份与能力清单可用 `read_agent_context`。'
        '工具不可用就按清单读取本地记忆索引，不去原笔记库。\n'
        '这里仅有交接快照，不含原会话、完整仓库或凭据；文件、命令和联网权限以'
        '当前会话设置为准。不把未迁移的 vococo MCP 当成可用；'
        '对外或破坏性操作先简要说明再执行。\n'
    )
    docs['context/agent-context.json'] = json.dumps(meta, ensure_ascii=False, indent=2)
    docs['context/cli-settings.json'] = json.dumps(_cli_settings(server_name))
    docs['.agents/mcp_config.json'] = json.dumps({'mcpServers': {server_name: {
        'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--serve', str(workspace)],
    }}})
    # 配置变化后，本轮不再生成的交接文件要清掉，免得旧记忆/旧规则继续被 MCP 读到。
    for name in _CONTEXT_FILES:
        with suppress(OSError):
            if f'context/{name}' not in docs:
                (workspace / 'context' / name).unlink()
    # 技能快照也要跟着 Agent 名单走：移出名单的旧目录会让 agy 原生技能发现继续列出它。
    current_skills = {item['name'] for item in meta['skills']}
    skills_root = workspace / '.agents/skills'
    if not skills_root.is_symlink() and skills_root.is_dir():
        for folder in skills_root.iterdir():
            if (re.fullmatch(r'[a-z0-9][a-z0-9-]*', folder.name) and folder.name not in current_skills
                    and folder.is_dir() and not folder.is_symlink()):
                with suppress(OSError):
                    shutil.rmtree(folder)
    for relative, text in docs.items():
        target = workspace / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return meta


def _portable_skill_from_docs(docs, name):
    prefix = f'.agents/skills/{name}/'
    files = {path[len(prefix):]: text for path, text in docs.items() if path.startswith(prefix)}
    return files if files.get('SKILL.md') else {}


def _skill_files(workspace: Path):
    root = workspace / '.agents/skills'
    if root.is_symlink() or not root.is_dir():
        return {}
    found = {}
    for folder in root.iterdir():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        text = _small_text(folder / 'SKILL.md')
        if text is not None:
            found[folder.name] = text
    return found


def _serve(workspace: Path):
    from mcp.server.mcpserver import MCPServer

    path = workspace / 'context/agent-context.json'
    payload = _small_text(path)
    if payload is None:
        raise ValueError('缺少交接快照')
    memory = {name: text for name in ('MEMORY.md', 'AGENT-MEMORY.md')
              if (text := _small_text(path.parent / name))}
    try:
        allowed = set(json.loads(payload).get('native_skills') or ())
    except (ValueError, AttributeError, TypeError):
        allowed = set()
    # 工作区会按轮刷新；技能从 Agent 名单移除后，旧目录不再对外暴露（不删除文件）。
    skills = {name: text for name, text in _skill_files(workspace).items() if name in allowed}
    server = MCPServer('vococo-context')

    @server.tool()
    def read_agent_context() -> str:
        """读取本次交接的 Agent ID 和能力清单，无需路径，无写权限。"""
        return payload

    @server.tool()
    def read_agent_memory() -> dict[str, str]:
        """只读: 返回本次可用的全局分区与 Agent 记忆索引；不读原笔记库。"""
        return memory

    @server.tool()
    def read_context_file(name: str) -> str:
        """只读: 读取本次交接的项目规则/Agent 职责/说明文件（固定名单，不支持任意路径）。"""
        if not isinstance(name, str) or name not in _CONTEXT_FILES:
            raise ValueError('未找到该交接文件')
        text = _small_text(path.parent / name)
        if text is None:
            raise ValueError('未找到该交接文件')
        return text

    @server.tool()
    def list_skills() -> list[dict[str, str]]:
        """只读: 列出本次交接可用的 Skill 名称与说明。"""
        return [{'name': name, 'description': _skill_description(text)} for name, text in skills.items()]

    @server.tool()
    def read_skill(name: str) -> str:
        """只读: 按名称读取本次交接的技能正文（SKILL.md）。"""
        if not isinstance(name, str) or name not in skills:
            raise ValueError('未找到该 Skill')
        return skills[name]

    server.run(transport='stdio')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--serve':
    _serve(Path(sys.argv[2]))
