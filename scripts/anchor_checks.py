#!/usr/bin/env python3
"""原地接替 13f146ce；固定中文模板直出，异常也不调用模型。"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from anchor_report import report

ROOT = Path(__file__).resolve().parent
RAW_DIR = Path.home() / ".local/state/anchor-mail/reports"
SIGNAL = re.compile(r"^##CRON_SIGNAL:[01]##\s*$", re.M)


def parse_result(result):
    try:
        data = json.loads(SIGNAL.sub("", result.get("stdout", "")).strip())
        if not isinstance(data, dict):
            raise ValueError("回执不是对象")
        return {**result, "data": data}
    except (ValueError, TypeError):
        return {**result, "data": None, "error": "JSON回执缺失或无效"}


def run_stage(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        stderr = exc.stderr or b""
        return {"stdout": output.decode(errors="replace") if isinstance(output, bytes) else output,
                "stderr": stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
                "returncode": None, "error": "TimeoutExpired"}
    except OSError as exc:
        return {"stdout": "", "stderr": "", "returncode": None, "error": type(exc).__name__}


def collect_results():
    commands = {
        "ceramic": [sys.executable, str(ROOT / "anchor_ceramic_scan.py"), "--scan"],
        "gmail": [sys.executable, str(ROOT / "anchor_gmail_scan.py"), "--apply"],
        "monitor": [sys.executable, str(ROOT / "anchor_monitor.py")],
    }
    return {stage: run_stage(command) for stage, command in commands.items()}


def save_raw(results, directory, checked_at):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = checked_at.replace(":", "-")
    path = directory / f"{stamp}-{os.getpid()}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"checked_at": checked_at, "results": results}, stream, ensure_ascii=False, indent=2)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="只读回执文件；不启动任何扫描、网络或邮件操作")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    args = parser.parse_args(argv)
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        results = json.loads(args.fixture.read_text()) if args.fixture else collect_results()
        results = {stage: parse_result(results.get(stage, {})) for stage in ("ceramic", "gmail", "monitor")}
        try:
            raw_path = save_raw(results, args.raw_dir, checked_at)
            archive_error = None
        except OSError as exc:
            raw_path, archive_error = "未保存", type(exc).__name__
        text, failed = report(results, checked_at, raw_path)
        if archive_error:
            text += f"\n- 原始回执存档失败：{archive_error}；以上可用数据保留。"
            failed = True
    except Exception as exc:
        text, failed = f"巡检报告生成/存档失败：{type(exc).__name__}；不能报告零询盘，未确认完整覆盖。", True
    print(text)
    # 信号只控制是否调用总结；退出码单独、如实控制 cron error 状态。
    print("##CRON_SIGNAL:0##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
