#!/usr/bin/env python3
"""原地接替 13f146ce 的调度命令；保留陶纤原扫描，不另建重复任务。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    commands = [
        [sys.executable, str(Path.home() / "scripts/raychen_gmail_daily.py"), "--scan"],
        [sys.executable, str(ROOT / "anchor_gmail_scan.py"), "--apply"],
        [sys.executable, str(ROOT / "anchor_monitor.py")],
    ]
    failed, alert = False, False
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            lines = result.stdout.splitlines()
            failed |= result.returncode != 0
            alert |= "##CRON_SIGNAL:1##" in lines
            print("\n".join(line for line in lines if not line.startswith("##CRON_SIGNAL:")))
            if result.returncode:
                print(f"脚本 {Path(command[1]).name} 退出码 {result.returncode}，本段未验收成功")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"脚本 {Path(command[1]).name} 未完成：{type(exc).__name__}")
            failed = True
    print("陶纤沿用旧扫描；其静默结果不代表列表分页/元数据读取完整覆盖。")
    print(f"##CRON_SIGNAL:{int(failed or alert)}##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
