#!/usr/bin/env python3
"""原地接替 13f146ce 的调度命令；保留陶纤原扫描，不另建重复任务。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    commands = [
        [sys.executable, str(ROOT / "anchor_ceramic_scan.py"), "--scan"],
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
                if result.stderr:
                    print(result.stderr[-2000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"脚本 {Path(command[1]).name} 未完成：{type(exc).__name__}")
            failed = True
    print("陶纤沿用原状态和水位；三箱均先排养号再核对已发邮件引用，未知来源待人工。")
    print(f"##CRON_SIGNAL:{int(failed or alert)}##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
