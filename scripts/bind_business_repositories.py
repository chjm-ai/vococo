"""业务仓库绑定初始化；默认预览，--apply 备份配置并准备主会话副本。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "offline-repository-binding-no-model")

from scripts.migrate_business_workspaces import apply_change
from vococo import config
from vococo.core import execution
from vococo.core.business_repositories import normalize_repositories
from vococo.memory import agents


def changes(agent_root: Path, bindings: list[str]) -> list[tuple[Path, dict, bytes]]:
    planned = {}
    for item in bindings:
        aid, path = item.split("=", 1)
        if not agents.valid_id(aid) or aid == agents.GENERAL_ID:
            raise ValueError(f"业务 Agent id 无效：{aid}")
        target = agent_root / aid / "agent.json"
        original = target.read_bytes()
        meta = planned.get(target, (json.loads(original), original))[0]
        if execution.execution_mode(meta.get("workdir"), meta.get("execution_mode")) != "workspace":
            raise ValueError(f"此初始化只用于业务 Agent：{aid}")
        meta["repositories"] = normalize_repositories([*meta.get("repositories", []), path])
        planned[target] = (meta, original)
    return [(path, meta, original) for path, (meta, original) in planned.items()]


async def prepare(aid: str) -> None:
    owner = agents.get(aid)
    policy = execution.policy_for_session(agents.main_session_key(owner))
    await execution.prepare_repositories(policy)
    for binding in policy.repositories:
        print(f"  已准备独立副本：{binding.checkout}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--bind", action="append", required=True, metavar="AGENT_ID=仓库根目录")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config.DATA_DIR = args.data_dir.expanduser().resolve()
    planned = changes(agents.root_dir(), args.bind)
    backup = config.DATA_DIR / "repository_binding_backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for path, meta, original in planned:
        print(f"{meta.get('name', path.parent.name)}：{', '.join(meta['repositories'])}")
        if args.apply:
            if json.loads(original) != meta:
                apply_change(path, meta, original, backup)
                print(f"  原配置备份：{backup / path.parent.name / path.name}")
            asyncio.run(prepare(path.parent.name))
    print("已初始化；未推送、未发布。" if args.apply else "仅预览，未修改配置。")


if __name__ == "__main__":
    main()
