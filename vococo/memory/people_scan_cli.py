"""人脉画像扫描的业务 CLI 入口(cron 任务与手动共用)。

用法(在任意环境,含任务 worktree):
    PYTHONPATH=<主仓库> <主仓库>/.venv/bin/python -m vococo.memory.people_scan_cli \
        --scan [--all] [--settings <web_settings.json>]

为什么是薄壳而不是直接在 people_profiles.py 加 __main__:导入 vococo.config
时,若设置页没有可用第三方供应商会强制要求 CLAUDE_CODE_OAUTH_TOKEN(worktree
/无 .env 环境直接 ConfigError)。本业务走 DeepSeek 第三方端点,不需要订阅
token——薄壳先设一个兜底 token 再导入,后续逻辑照常。

--settings:worktree 场景 settings 文件不在本地(data/ 是空的),传主仓库的
data/web_settings.json(只读);省略则用本环境默认路径。
水位/待确认清单落在 config.PEOPLE_PROFILES_WATERMARK/AI_BRAIN(后台任务
唯一可写豁免目录),任何环境都能写。
"""
from __future__ import annotations

import os
import sys

# 必须在导入 vococo.* 之前设置:config 模块加载时就要读这个 env
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "cli-people-scan")

import argparse  # noqa: E402
from pathlib import Path  # noqa: E402

import anyio  # noqa: E402


def _print_summary(summaries: list[dict]) -> None:
    done = [s for s in summaries if s.get("status") == "done"]
    pending = [s for s in summaries if s.get("status") == "pending"]
    skipped = [s for s in summaries if s.get("status") == "skipped"]
    error = [s for s in summaries if s.get("status") == "error"]
    if not summaries:
        print("没有新/改动的笔记,画像无更新。")
        return
    print(
        f"共处理 {len(summaries)} 篇笔记:更新 {len(done)} / 待确认 {len(pending)}"
        f" / 无人物跳过 {len(skipped)} / 错误 {len(error)}"
    )
    for s in done:
        print(f"  · {s['note']}: {', '.join(s['updated'])}")
    for s in pending[:5]:
        print(f"  ⚠ 待确认「{s['note']}」没识别出具体人物")
    if len(pending) > 5:
        print(f"  …还有 {len(pending) - 5} 篇待确认")
    for s in error[:3]:
        print(f"  ✗ {s['note']}: {s.get('reason')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描 Obsidian 笔记更新人脉画像")
    ap.add_argument("--scan", action="store_true", help="增量扫描(默认动作)")
    ap.add_argument("--all", action="store_true", help="清水位全量重扫(耗时 10 分钟+)")
    ap.add_argument(
        "--settings", default=None,
        help="settings 文件路径(worktree 场景传主仓库 data/web_settings.json)",
    )
    args = ap.parse_args()

    if args.settings:
        from vococo.gateway import settings_store

        settings_store._PATH = Path(args.settings)

    from vococo.memory import people_profiles as pp

    if args.all:
        pp._obsidian_watermark_path().write_text("{}", encoding="utf-8")
    summaries = anyio.run(pp.scan_obsidian_notes)
    _print_summary(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
