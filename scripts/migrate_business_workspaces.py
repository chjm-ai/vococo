"""迁移业务执行策略；默认仅预览，--apply 才备份并更新配置。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 此离线迁移不启动模型，不应要求订阅凭据；占位值仅限本脚本进程。
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "offline-workspace-migration-no-model")
from vococo import config
from vococo.core.execution import execution_mode


def changes(agent_root: Path, overrides: dict[str, str], writable: dict[str, str] | None = None) -> list[tuple[Path, dict, bytes]]:
    result = []
    for path in sorted(agent_root.glob("*/agent.json")):
        original = path.read_bytes()
        meta = json.loads(original)
        aid, workdir = path.parent.name, meta.get("workdir")
        if aid == "general" or not workdir or not Path(workdir).is_dir():
            continue
        updated = dict(meta)
        updated["execution_mode"] = execution_mode(workdir, meta.get("execution_mode"))
        if aid in overrides:
            updated.update(execution_mode="workspace", output_dir=overrides[aid])
        if aid in (writable or {}):
            target = writable[aid]
            links = list(updated.get("links") or [])
            if not any(link.get("path") == target and link.get("writable") for link in links):
                links.append({"path": target, "writable": True, "note": "业务工具运行数据"})
                updated["links"] = links
        if updated != meta:
            result.append((path, updated, original))
    return result


def apply_change(path: Path, value: dict, original: bytes, backup_dir: Path) -> None:
    """备份原件；写前检查避免覆盖预览后发生的配置变更。"""
    if path.read_bytes() != original:
        raise RuntimeError(f"规划后配置已变更，停止迁移：{path}")
    destination = backup_dir / path.parent.name / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    temporary = path.with_suffix(".json.workspace-tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if path.read_bytes() != original:
        temporary.unlink()
        raise RuntimeError(f"配置已被其他会话修改，停止迁移：{path}")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--workspace-agent", action="append", default=[], metavar="ID=成果目录")
    parser.add_argument("--writable-dir", action="append", default=[], metavar="ID=数据目录")
    args = parser.parse_args()
    overrides = {key: str(Path(value).expanduser().resolve()) for key, value in
                 (item.split("=", 1) for item in args.workspace_agent)}
    writable = {key: str(Path(value).expanduser().resolve()) for key, value in
                (item.split("=", 1) for item in args.writable_dir)}
    planned = changes(args.data_dir / "agents", overrides, writable)
    backup = args.data_dir / "workspace_migration_backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for path, value, original in planned:
        print(f"{value.get('name', path.parent.name)}: {value['execution_mode']}，成果={value.get('output_dir') or value['workdir']}")
        if args.apply:
            apply_change(path, value, original, backup)
    print(f"{'已更新' if args.apply else '预览'} {len(planned)} 项 Agent 配置；定时脚本保持原目录，不移动历史产物。")
    if args.apply and planned:
        print(f"原配置备份：{backup}")


if __name__ == "__main__":
    main()
