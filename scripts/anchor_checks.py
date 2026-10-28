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

from anchor_report import LINES, event, report

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
        json.dump({"checked_at": checked_at, "run_id": os.environ.get("VOCOCO_CRON_RUN_ID"),
                   "results": results}, stream, ensure_ascii=False, indent=2)
    return path


def bounce_counts(results):
    monitor = results.get("monitor", {})
    if not isinstance(monitor, dict) or monitor.get("returncode") != 0 or monitor.get("error"):
        return {}
    data = monitor.get("data")
    campaigns = data.get("campaigns") if isinstance(data, dict) else None
    if not isinstance(campaigns, dict):
        return {}
    return {name: event(row, "emailsBounced", "events_all")
            for name in LINES if isinstance((row := campaigns.get(name)), dict)}


def bounce_alerts(results, directory, before_path=None):
    try:
        paths = sorted((path for path in directory.glob("*.json")
                        if before_path is None or path.name < before_path.name), reverse=True)
        if not paths:
            raise ValueError("无上次存档")
        archive = json.loads(paths[0].read_text(encoding="utf-8"))
        previous = bounce_counts(archive["results"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        previous = {}
    current = bounce_counts(results)
    alerts = []
    for name in LINES:
        before, after = previous.get(name), current.get(name)
        if before is None or after is None:
            alerts.append(f"{name}：退信增量未知（上次基线或本次累计退信缺失/损坏），需核查。")
        elif after < before:
            alerts.append(f"{name}：累计退信从 {before} 降至 {after}，增量未知，需核查。")
        elif after > before:
            alerts.append(f"{name}：新增退信事件 {after - before}（累计 {before} → {after}）。")
    return alerts


def replay_pending(directory):
    pending = json.loads(os.environ.get("VOCOCO_CRON_PENDING_RUN_IDS", "[]"))
    if not isinstance(pending, list) or not all(isinstance(item, str) for item in pending):
        raise ValueError("未确认运行列表无效")
    if not pending:
        return ""
    archives = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("run_id") in pending:
                archives[data["run_id"]] = (path, data)
        except (OSError, ValueError, TypeError):
            continue
    messages = []
    for run_id in pending:
        try:
            path, data = archives[run_id]
            text, _ = report(data["results"], data["checked_at"], path)
            alerts = bounce_alerts(data["results"], directory, before_path=path)
            messages.append(f"### 未确认运行 {run_id}\n{text}\n退信核查：\n" +
                            "\n".join(f"- {alert}" for alert in alerts or ["累计退信未增加"]))
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            messages.append(f"### 未确认运行 {run_id}\n回执缺失或损坏，转发和退信情况未知，请人工核查。")
    return "## 上次通知未确认，保守重播（不重新转发）\n" + "\n".join(messages)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="只读回执文件；不启动任何扫描、网络或邮件操作")
    parser.add_argument("--raw-dir", type=Path)
    args = parser.parse_args(argv)
    if args.fixture:
        raw = args.raw_dir.resolve() if args.raw_dir else None
        production = RAW_DIR.resolve()
        if raw is None or raw == production or production in raw.parents or raw in production.parents:
            parser.error("fixture 必须指定独立的 --raw-dir")
    args.raw_dir = args.raw_dir or RAW_DIR
    checked_at = datetime.now(timezone.utc).isoformat()
    replay = ""
    try:
        replay = replay_pending(args.raw_dir)
        results = json.loads(args.fixture.read_text()) if args.fixture else collect_results()
        results = {stage: parse_result(results.get(stage, {})) for stage in ("ceramic", "gmail", "monitor")}
        alerts = bounce_alerts(results, args.raw_dir)
        try:
            raw_path = save_raw(results, args.raw_dir, checked_at)
            archive_error = None
        except OSError as exc:
            raw_path, archive_error = "未保存", type(exc).__name__
        text, failed = report(results, checked_at, raw_path)
        if alerts:
            text += "\n## 退信告警\n" + "\n".join(f"- {alert}" for alert in alerts)
        if archive_error:
            text += f"\n- 原始回执存档失败：{archive_error}；以上可用数据保留。"
            failed = True
        ceramic = results["ceramic"]["data"] or {}
        gmail = results["gmail"]["data"] or {}
        forwards = bool(ceramic.get("submitted_forward_ids")) or any(
            isinstance(row, dict) and isinstance(row.get("counts"), dict) and row["counts"].get("sent")
            for row in gmail.values()
        )
        notify = failed or forwards or bool(alerts)
    except Exception as exc:
        text, failed, notify = f"巡检报告生成/存档失败：{type(exc).__name__}；不能报告零询盘，未确认完整覆盖。", True, True
    if replay:
        print(replay)
    if notify:
        print(text)
    # 信号只控制是否调用总结；退出码单独、如实控制 cron error 状态。
    print("##CRON_SIGNAL:0##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
