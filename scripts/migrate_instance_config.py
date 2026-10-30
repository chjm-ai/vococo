#!/usr/bin/env python3
"""从指定旧 Git 版本提取实例配置；只新建私有配置，不导入旧代码、不连接服务。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def source(ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def literal(text: str, name: str):
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"旧版本缺少 {name}，拒绝猜测迁移")


def extract(ref: str) -> dict:
    brain = source(ref, "vococo/gateway/brain_routes.py")
    frontend = source(ref, "vococo/gateway/brain_mcp.py")
    preview = source(ref, "vococo/gateway/adapters/web_static/preview.js")
    publish = source(ref, "vococo/tools/publishing_kimmy.py")
    persona = source(ref, "vococo/core/prompt.py")
    agents = source(ref, "vococo/memory/agents.py")
    engine = source(ref, "vococo/core/agent.py")
    config = source(ref, "vococo/config.py")
    remote = publish.split("_REMOTE = r'''", 1)[1].split("'''", 1)[0]
    matched = re.search(r'new URL\(share.path,"(https://[^\"]+)"\)', preview)
    origin = re.search(r'allowed_origins=\["(https://[^\"]+)"\]', frontend)
    if not matched or not origin:
        raise ValueError("旧版本域名格式变化，须人工核对")
    target = {
        "ssh_host": re.search(r'"ConnectTimeout=10", "([^\"]+)"', publish)[1],
        "public_origin": re.search(r'https://[^/\s\"]+/\{slug\}/', publish)[0].split('/{slug}/')[0],
        "deploy_root": re.search(r'root = Path\("([^\"]+)"\)', remote)[1],
        "web_root": re.search(r'live = root / "incoming" / slug, Path\("([^\"]+)"\)', remote)[1],
        "snippet_root": re.search(r'snippet = Path\("([^\"]+)"\)', remote)[1],
        "script_sha256": literal(publish, "_DEPLOY_SHA256"),
        "contract_lines": sorted(literal(remote, "expected")),
    }
    layout = persona.split('- 回复使用 Markdown；', 1)[1].split('\n\n=== 记忆工具', 1)[0]
    execution = persona.split('=== 执行方式(重要)===\n', 1)[1].split('- 以下这条专指 Agent 子代理工具:', 1)[0]
    folders = re.search(r'"PEOPLE_PROFILES_OBSIDIAN_FOLDERS",\s*"([^\"]+)"', config)[1]
    return {"version": 1,
            "brain": {"agent_id": literal(brain, "AGENT_ID"), "origin": origin[1]},
            "publishing": {"share_origin": matched[1], "kimmy": target,
                           "legacy_kimmy_fingerprint": fingerprint(target)},
            "preferences": {"persona": '- 回复使用 Markdown；' + layout + '\n\n=== 执行方式(重要)===\n' + execution.rstrip(),
                            "memory_sections": list(literal(agents, "DEFAULT_MEMORY_SECTIONS")),
                            "memory_rules": literal(agents, "MEMORY_RULES"),
                            "decision_rules": literal(agents, "DECISION_RULES"),
                            "people_folders": folders.split(',')},
            "mcp": {"task_keywords": literal(engine, "_MCP_TASK_KEYWORDS"),
                    "groups": {"外贸工具包": "all"}}}


def fingerprint(target: dict) -> str:
    return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_new(path: Path, data: dict) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("配置路径不能包含软链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    # x + 0600：不覆盖已有配置，也不留下可被其他用户读取的中间窗口。
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-ref', required=True, help='修改前的 Git 提交或分支')
    parser.add_argument('--output', type=Path, required=True, help='新配置绝对路径；存在则拒绝覆盖')
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error('--output 必须是绝对路径')
    write_new(args.output, extract(args.source_ref))
    print(f"已新建实例配置：{args.output}（未连接服务、未改生产库）")


if __name__ == '__main__':
    main()
