#!/usr/bin/env python3
"""编码任务对比评测:同一任务,Hermes vs Claude Code,量化谁做得好。

为什么这么设计(见 memory/hermes-eval-harness.md):纯 QA 考不出差异,真正拉开差距的是
harness 能力 —— 编码任务(多步 + 工具 + 真改代码)正好能考出来。

公平性三保证:
  1. 同模型(默认 claude-sonnet-5,两边都走它)。
  2. 同起点:每次 run 前 `git reset --hard <baseline>` + `git clean -fd`,两边从同一状态出发。
  3. 同 prompt。

跑法:
  python eval/run_coding_bench.py --tasks eval/coding_tasks.json
  python eval/run_coding_bench.py --only fix-x --tools hermes         # 只跑某任务/某一边
  python eval/run_coding_bench.py --dry-run                           # 只打印将要做什么,不真跑

采集(每 任务 × 每 工具):验收是否通过、墙钟秒数、改了几个文件/增删行数、输出字数、报错。
结果矩阵打印到终端并存 eval/results/<时间戳>.md。

⚠️ 会真的改目标 repo 的工作区(在受控的 git reset 之间),请只对一次性的评测 repo/分支跑,
   别对你正在写的分支跑。跑 Claude Code 会消耗订阅额度。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERMES_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(HERMES_ROOT / ".venv" / "bin" / "python")
if not Path(PYTHON).exists():
    PYTHON = sys.executable

# 用 stdin 喂 prompt,cwd=目标 repo,让 Hermes agent 在该 repo 里干活(SDK 用进程 cwd)
_HERMES_DRIVER = f"""
import sys, asyncio
sys.path.insert(0, {str(HERMES_ROOT)!r})
from claude_hermes.core.agent import run_turn
q = sys.stdin.read().strip()
r = asyncio.run(run_turn([], q))
print(r.text or "")
"""


def _run(cmd: list[str], cwd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


def reset_repo(repo: str, baseline: str) -> None:
    """把 repo 硬重置到 baseline 并清掉未跟踪文件 —— 两边同一起点。"""
    _run(["git", "reset", "--hard", baseline], repo)
    _run(["git", "clean", "-fd"], repo)


def diff_stat(repo: str, baseline: str) -> dict:
    """相对 baseline 的改动量:文件数 / 增 / 删。"""
    out = _run(["git", "diff", "--shortstat", baseline], repo).stdout.strip()
    files = ins = dele = 0
    import re

    if m := re.search(r"(\d+) files? changed", out):
        files = int(m.group(1))
    if m := re.search(r"(\d+) insertions?", out):
        ins = int(m.group(1))
    if m := re.search(r"(\d+) deletions?", out):
        dele = int(m.group(1))
    return {"files": files, "insertions": ins, "deletions": dele}


def verify(repo: str, cmd: str, timeout: int) -> bool:
    """跑验收命令,exit 0 = 通过。"""
    if not cmd:
        return False
    try:
        r = subprocess.run(cmd, cwd=repo, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def run_hermes(repo: str, prompt: str, model: str, timeout: int) -> dict:
    env = {**os.environ, "AGENT_MODEL": model}
    t0 = time.time()
    try:
        r = subprocess.run(
            [PYTHON, "-c", _HERMES_DRIVER], cwd=repo, input=prompt,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return {"seconds": round(time.time() - t0, 1), "output_chars": len(r.stdout), "error": r.stderr[-300:] if r.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"seconds": timeout, "output_chars": 0, "error": "TIMEOUT"}


def run_claude_code(repo: str, prompt: str, model: str, timeout: int) -> dict:
    if shutil.which("claude") is None:
        return {"seconds": 0, "output_chars": 0, "error": "SKIP: 未找到 claude CLI"}
    t0 = time.time()
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--permission-mode", "bypassPermissions"],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
        )
        return {"seconds": round(time.time() - t0, 1), "output_chars": len(r.stdout), "error": r.stderr[-300:] if r.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"seconds": timeout, "output_chars": 0, "error": "TIMEOUT"}


RUNNERS = {"hermes": run_hermes, "claude_code": run_claude_code}


def run_one(task: dict, tool: str, model: str, timeout: int, dry: bool) -> dict:
    repo, baseline = task["repo"], task.get("baseline", "HEAD")
    row = {"task": task["id"], "tool": tool}
    if dry:
        print(f"  [dry] {tool}: reset {repo}→{baseline}; 跑 prompt({len(task['prompt'])}字); verify: {task.get('verify','—')}")
        return {**row, "verify": "-", "seconds": 0, "files": 0, "insertions": 0, "deletions": 0, "error": "dry-run"}
    reset_repo(repo, baseline)
    res = RUNNERS[tool](repo, task["prompt"], model, timeout)
    stat = diff_stat(repo, baseline)
    ok = verify(repo, task.get("verify", ""), task.get("verify_timeout", 180))
    reset_repo(repo, baseline)  # 收尾还原,不留改动
    return {**row, "verify": "✅" if ok else "❌", **res, **stat}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(Path(__file__).parent / "coding_tasks.json"))
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--tools", default="hermes,claude_code", help="逗号分隔:hermes,claude_code")
    ap.add_argument("--only", default="", help="只跑某个 task id")
    ap.add_argument("--timeout", type=int, default=900, help="单次运行墙钟上限(秒)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]
    tools = [t.strip() for t in args.tools.split(",") if t.strip() in RUNNERS]

    rows: list[dict] = []
    for task in tasks:
        print(f"\n▶ 任务 {task['id']}: {task.get('title', '')}")
        for tool in tools:
            print(f"  跑 {tool} …")
            rows.append(run_one(task, tool, args.model, args.timeout, args.dry_run))

    # 结果矩阵
    header = "| 任务 | 工具 | 验收 | 秒 | 文件 | +行 | -行 | 备注 |"
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        note = (r.get("error") or "")[:40]
        lines.append(
            f"| {r['task']} | {r['tool']} | {r.get('verify','-')} | {r.get('seconds',0)} | "
            f"{r.get('files',0)} | {r.get('insertions',0)} | {r.get('deletions',0)} | {note} |"
        )
    table = "\n".join(lines)
    print("\n" + table)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(__file__).parent / "results" / f"{stamp}.md"
    out.write_text(
        f"# 编码评测 {stamp}\n\n模型:{args.model} · 工具:{tools}\n\n{table}\n", encoding="utf-8"
    )
    print(f"\n📄 结果已存 {out}")


if __name__ == "__main__":
    main()
