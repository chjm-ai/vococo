"""巡检展示离线验收：fixture 不允许启动扫描或连接真实邮箱。"""
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import anchor_checks as checks
import anchor_daily as daily
import anchor_report as template


def receipt(data, code=0):
    return {"stdout": json.dumps(data, ensure_ascii=False) + "\n##CRON_SIGNAL:1##\n",
            "stderr": "", "returncode": code}


def sample():
    scan = {"coverage": "ok", "counts": {}, "manual_review_ids": [], "manual_review": {}, "errors": []}
    ceramic = {"coverage": "ok", "submitted_forward_ids": [], "manual_review_ids": [],
               "manual_review": {}, "pending": [], "errors": []}
    campaigns = {}
    for name, leads, sent in (("陶纤", 225, 14), ("过滤管", 1, 1), ("微孔板", 3, 3)):
        campaigns[name] = {"status": "running", "leads": leads, "warnings": [],
                           "emailsSent": {"events_day": sent, "events_all": 576 if leads == 225 else sent,
                                          "leads_all": leads},
                           "emailsBounced": {"events_day": 0, "events_all": 4 if leads == 225 else 0,
                                             "leads_all": 4 if leads == 225 else 0},
                           "emailsReplied": {"events_day": 0, "events_all": 8 if leads == 225 else 0,
                                             "leads_all": 6 if leads == 225 else 0}}
    return {"ceramic": receipt(ceramic), "gmail": receipt({address: dict(scan) for address in template.LINES.values() if address}),
            "monitor": receipt({"checked_at": "2026-10-27T10:24:19Z", "day_cn": "2026-10-26", "campaigns": campaigns})}


def render(results):
    parsed = {stage: checks.parse_result(row) for stage, row in results.items()}
    return template.report(parsed, "2026-10-27T10:24:20Z", "fixture.json")


def change(results, stage, mutate):
    data = checks.parse_result(results[stage])["data"]
    mutate(data)
    results[stage] = receipt(data, results[stage]["returncode"])


def partial_sample():
    results = sample()
    def partial(data):
        for address, size in (("ray@anchor-filter.com", 3), ("ray@anchor-microporous.com", 4)):
            data[address].update(coverage="partial", counts={"old": 1}, manual_review_ids=[str(i) for i in range(size)],
                                 manual_review={str(i): {"subject": "Re: storyboard lemwarmup ABC123",
                                                         "note": "引用关联不证明采购意向"} for i in range(size)})
    change(results, "gmail", partial)
    results["gmail"]["returncode"] = 1
    return results


def test_normal_distinguishes_dates_events_and_cumulative():
    text, failed = render(sample())
    assert not failed
    for phrase in ("2026-10-26", "2026-10-27T10:24:19Z", "14 / 0 / 0", "576 / 4 / 8", "4/225 = 1.78%",
                   "含跟进", "不是新增人数", "累计回复不等于有效询盘", "陶纤仅描述扫描回执"):
        assert phrase in text
    assert '"events_day"' not in text


def test_partial_manual_stock_is_not_new_or_inquiry():
    text, failed = render(partial_sample())
    assert failed
    assert "过滤管：扫描不完整（partial）" in text
    assert "微孔板：扫描不完整（partial）" in text
    assert "其中 3 封主题带 lemwarmup" in text and "其中 4 封主题带 lemwarmup" in text
    assert "存量待人工 4" in text and "本次转人工 未知" in text
    assert "不能据此报告零询盘" in text and "不可算客户询盘" in text
    assert "退出码 1" in text
    assert render(partial_sample())[0] == text  # 不累计上轮的待人工。


@pytest.mark.parametrize("stage", ["ceramic", "gmail", "monitor"])
def test_nonzero_exit_keeps_other_lines(stage):
    results = sample()
    results[stage]["returncode"] = 7
    text, failed = render(results)
    assert failed and "退出码 7" in text
    assert "14 / 0 / 0" in text and "微孔板" in text


def test_missing_fields_do_not_become_zero():
    results = sample()
    change(results, "monitor", lambda data: data["campaigns"]["过滤管"].pop("emailsSent"))
    text, failed = render(results)
    assert failed and "未知 / 0 / 0" in text
    assert "过滤管：活动字段缺失" in text
    assert "微孔板：活动字段缺失" not in text


def test_api_failure_retains_usable_line_and_partial_fields():
    results = sample()
    def fail(data):
        data["campaigns"]["过滤管"] = {"coverage": "failed", "error": "HTTPError", "leads": 1}
    change(results, "monitor", fail)
    text, failed = render(results)
    assert failed and "过滤管：campaign API 读取失败" in text
    assert "| 过滤管 | 未知 | 1 | 未知 / 未知 / 未知" in text
    assert "| 微孔板 | running | 3 | 3 / 0 / 0" in text


@pytest.mark.parametrize("stage", ["ceramic", "gmail", "monitor"])
def test_invalid_json_and_timeout_keep_other_receipts(stage):
    results = sample()
    results[stage] = {"returncode": None, "stdout": "{", "error": "TimeoutExpired"}
    text, failed = render(results)
    assert failed and f"{stage} 脚本未成功" in text
    assert "三线数据" in text


def test_fixture_entrypoint_has_zero_signal_and_preserves_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(partial_sample()))
    monkeypatch.setattr(checks, "collect_results", lambda: pytest.fail("fixture 不得启动扫描"))
    assert checks.main(["--fixture", str(path), "--raw-dir", str(tmp_path / "raw")]) == 1
    text = capsys.readouterr().out
    assert text.endswith("##CRON_SIGNAL:0##\n") and text.count("##CRON_SIGNAL:") == 1
    raw = next((tmp_path / "raw").glob("*.json"))
    assert raw.stat().st_mode & 0o777 == 0o600
    assert json.loads(raw.read_text())["results"]["gmail"]["returncode"] == 1
    assert '"manual_review_ids"' not in text


def test_real_cli_fixture_path_is_side_effect_free(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(sample()))
    checks.save_raw({stage: checks.parse_result(row) for stage, row in sample().items()},
                    tmp_path / "raw", "2020-01-01T00:00:00+00:00")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/anchor_checks.py"), "--fixture", str(fixture),
                             "--raw-dir", str(tmp_path / "raw")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("##CRON_SIGNAL:0##\n")
    assert result.stdout == "##CRON_SIGNAL:0##\n"


def test_process_interrupted_after_archive_replays_bounce_and_forward_without_sending(tmp_path):
    import os
    fixture = tmp_path / "fixture.json"
    raw = tmp_path / "raw"
    original = sample()
    checks.save_raw({stage: checks.parse_result(row) for stage, row in original.items()}, raw, "2020")
    changed = sample()
    change(changed, "monitor", lambda data: data["campaigns"]["陶纤"]["emailsBounced"].update(events_all=5))
    change(changed, "ceramic", lambda data: data.update(submitted_forward_ids=["forward-1"]))
    fixture.write_text(json.dumps(changed))
    code = ("import os,sys; sys.path.insert(0,sys.argv[1]); import anchor_checks as c; "
            "save=c.save_raw; "
            "c.save_raw=lambda *a: (save(*a),os._exit(73))[0]; "
            "c.main(['--fixture',sys.argv[2],'--raw-dir',sys.argv[3]])")
    env = {**os.environ, "VOCOCO_CRON_RUN_ID": "interrupted"}
    proc = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"), str(fixture), str(raw)], env=env)
    assert proc.returncode == 73
    fixture.write_text(json.dumps({**original, "monitor": changed["monitor"]}))
    env.update(VOCOCO_CRON_RUN_ID="next", VOCOCO_CRON_PENDING_RUN_IDS='["interrupted"]')
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/anchor_checks.py"), "--fixture", str(fixture),
                           "--raw-dir", str(raw)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "保守重播" in proc.stdout and "新增退信事件 1" in proc.stdout
    assert "本次提交/核实转发记录 1" in proc.stdout
    assert "不重新转发" in proc.stdout


def test_missing_pending_receipt_warns_instead_of_silence(tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(sample()))
    monkeypatch.setenv("VOCOCO_CRON_PENDING_RUN_IDS", '["missing"]')
    checks.main(["--fixture", str(fixture), "--raw-dir", str(tmp_path / "raw")])
    text = capsys.readouterr().out
    assert "missing" in text and "回执缺失或损坏" in text


def test_archive_failure_keeps_available_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(checks, "collect_results", sample)
    monkeypatch.setattr(checks, "save_raw", lambda *a: (_ for _ in ()).throw(OSError()))
    assert checks.main(["--raw-dir", str(tmp_path)]) == 1
    text = capsys.readouterr().out
    assert "14 / 0 / 0" in text and "存档失败" in text


def test_stage_failure_does_not_stop_next_stage(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 600, output=b"partial")
        return SimpleNamespace(stdout="{}", stderr="", returncode=0)
    monkeypatch.setattr(checks.subprocess, "run", run)
    results = checks.collect_results()
    assert len(calls) == 3
    assert results["ceramic"]["error"] == "TimeoutExpired"
    assert results["ceramic"]["stdout"] == "partial"
    assert results["monitor"]["returncode"] == 0


def test_missing_dates_mark_failure():
    results = sample()
    change(results, "monitor", lambda data: data.pop("day_cn"))
    text, failed = render(results)
    assert failed and "day_cn 缺失或无效" in text


def test_partial_error_missing_counts_are_unknown_not_zero():
    results = sample()
    def fail(data):
        data["ray@anchor-filter.com"].update(coverage="partial", counts={}, errors=["OSError"])
    change(results, "gmail", fail)
    text, failed = render(results)
    assert failed and "本次提交/核实转发记录 未知，本次转人工 未知" in text


def test_reconciled_sent_is_not_claimed_as_new_submission():
    results = sample()
    def sent(data):
        data["ray@anchor-filter.com"]["counts"] = {"sent": 1}
    change(results, "gmail", sent)
    text, failed = render(results)
    assert not failed and "本次提交/核实转发记录 1" in text
    assert "上轮发送、本轮才核实" in text and "不等于本次新发送" in text


@pytest.mark.parametrize("bad_detail", [None, [], "invalid"])
def test_malformed_manual_detail_does_not_drop_other_lines(bad_detail):
    results = sample()
    def bad(data):
        data["ray@anchor-filter.com"].update(manual_review_ids=["m1"], manual_review={"m1": bad_detail})
    change(results, "gmail", bad)
    text, failed = render(results)
    assert failed and "详情缺失或格式错误" in text
    assert "| 微孔板 | running | 3 | 3 / 0 / 0" in text
    assert "14 / 0 / 0" in text


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("summary", ["", "旧配置也不得触发总结"])
async def test_real_scheduler_fixture_never_calls_llm(isolated, monkeypatch, partial, summary):
    from vococo.cron import scheduler
    from vococo.core import task_runner
    from vococo.memory import session_store

    fixture = isolated / "fixture.json"
    fixture.write_text(json.dumps(partial_sample() if partial else sample()))
    data = isolated / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", data / "cron_jobs.json")
    def forbidden(*args, **kwargs):
        pytest.fail("巡检运行和完成后都不得调用模型或派发Agent")
    monkeypatch.setattr(scheduler.providers, "sidecar_chat", forbidden)
    monkeypatch.setattr(task_runner, "dispatch", forbidden)
    checks.save_raw({stage: checks.parse_result(row) for stage, row in sample().items()},
                    isolated / "raw", "2020-01-01T00:00:00+00:00")
    command = shlex.join([sys.executable, str(ROOT / "scripts/anchor_checks.py"), "--fixture", str(fixture),
                          "--raw-dir", str(isolated / "raw")])
    job = scheduler.create_job(name="三线fixture", prompt="离线", schedule={"kind": "cron", "expr": "23 8-22/2 * * *"})
    job.update(id="13f146ce", mode="script", command=command, summarize_prompt=summary)
    scheduler.save_jobs([job])
    async def push(*args):
        pass
    await scheduler._run_script_job(job, push)
    saved = scheduler.load_jobs()[0]
    assert saved["last_status"] == ("error" if partial else "success")
    receipt = json.loads(sorted((isolated / "raw").glob("*.json"))[-1].read_text())
    assert receipt["run_id"] == saved["last_run_id"]
    assert not saved.get("running_run_id")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    if partial:
        with pytest.raises(ValueError, match="未成功"):
            daily.latest_report(isolated / "raw", now, data / "cron_jobs.json")
    else:
        assert not daily.latest_report(isolated / "raw", now, data / "cron_jobs.json")[1]
    turns = session_store.load_recent(job["conv"])
    if partial:
        text = turns[0].assistant
        assert "三线数据" in text and "CRON_SIGNAL" not in text
        assert "不可算客户询盘" in text and "退出码 1" in text
    else:
        assert not turns


def test_reply_and_malformed_receipt_are_never_silent(tmp_path, capsys):
    path = tmp_path / "fixture.json"
    results = sample()
    change(results, "ceramic", lambda data: data.update(submitted_forward_ids=["id"]))
    path.write_text(json.dumps(results))
    assert checks.main(["--fixture", str(path), "--raw-dir", str(tmp_path / "raw")]) == 0
    assert "本次提交/核实转发记录 1" in capsys.readouterr().out
    results["gmail"]["stdout"] = "invalid"
    path.write_text(json.dumps(results))
    assert checks.main(["--fixture", str(path), "--raw-dir", str(tmp_path / "raw")]) == 1
    assert "扫描回执缺失" in capsys.readouterr().out


def test_malformed_counts_are_failure():
    results = sample()
    change(results, "gmail", lambda data: data["ray@anchor-filter.com"].update(counts={"sent": "invalid"}))
    text, failed = render(results)
    assert failed and "扫描计数格式错误" in text


@pytest.mark.parametrize("name", list(template.LINES))
def test_new_bounce_alert_and_next_run_silent(tmp_path, capsys, name):
    fixture = tmp_path / "fixture.json"
    results = sample()
    fixture.write_text(json.dumps(results))
    args = ["--fixture", str(fixture), "--raw-dir", str(tmp_path / "raw")]
    assert checks.main(args) == 0
    assert "退信增量未知" in capsys.readouterr().out
    change(results, "monitor", lambda data: data["campaigns"][name]["emailsBounced"].update(events_all=9))
    fixture.write_text(json.dumps(results))
    assert checks.main(args) == 0
    assert f"{name}：新增退信事件" in capsys.readouterr().out
    assert checks.main(args) == 0
    assert capsys.readouterr().out == "##CRON_SIGNAL:0##\n"


@pytest.mark.parametrize("baseline", ["corrupt", "missing_count", "decreased"])
def test_bad_baseline_or_decrease_warns(tmp_path, capsys, baseline):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(sample()))
    raw = tmp_path / "raw"
    raw.mkdir()
    old = sample()
    if baseline == "missing_count":
        change(old, "monitor", lambda data: data["campaigns"]["过滤管"]["emailsBounced"].pop("events_all"))
    if baseline == "decreased":
        change(old, "monitor", lambda data: data["campaigns"]["过滤管"]["emailsBounced"].update(events_all=5))
    if baseline == "corrupt":
        (raw / "2020.json").write_text("{")
    else:
        checks.save_raw({stage: checks.parse_result(row) for stage, row in old.items()}, raw, "2020")
    assert checks.main(["--fixture", str(fixture), "--raw-dir", str(raw)]) == 0
    text = capsys.readouterr().out
    assert "过滤管：" in text and "增量未知" in text


def test_fixture_requires_own_raw_directory(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(sample()))
    with pytest.raises(SystemExit):
        checks.main(["--fixture", str(fixture)])
    with pytest.raises(SystemExit):
        checks.main(["--fixture", str(fixture), "--raw-dir", str(checks.RAW_DIR)])


def test_daily_uses_latest_raw_receipt_and_rejects_stale(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 28, 2, 43, tzinfo=timezone.utc)
    checked = now - timedelta(minutes=20)
    parsed = {stage: checks.parse_result(row) for stage, row in sample().items()}
    raw = tmp_path / "raw"
    monkeypatch.setenv("VOCOCO_CRON_RUN_ID", "run-1")
    checks.save_raw(parsed, raw, checked.isoformat())
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"id": "13f146ce", "last_status": "success", "last_run_at": now.timestamp(), "last_run_id": "run-1"}]))
    text, failed = daily.latest_report(raw, now, jobs)
    assert not failed and "三线数据" in text and checked.isoformat() in text
    with pytest.raises(ValueError, match="超过一小时"):
        daily.latest_report(raw, now + timedelta(hours=4), jobs)
    jobs.write_text(json.dumps([{"id": "13f146ce", "last_status": "error", "last_run_at": now.timestamp()}]))
    with pytest.raises(ValueError, match="未成功"):
        daily.latest_report(raw, now, jobs)
    jobs.write_text(json.dumps([{"id": "13f146ce", "last_status": "success", "last_run_id": "run-1", "last_run_at": (now + timedelta(hours=1)).timestamp()}]))
    with pytest.raises(ValueError, match="不匹配"):
        daily.latest_report(raw, now, jobs)


@pytest.mark.parametrize("last_id,raw_id,running", [
    (None, None, None), ("a", None, None), ("a", "b", None),
    ("a", "a", "b"), ("", "", None),
])
def test_daily_rejects_unbound_or_running_receipt(tmp_path, monkeypatch, last_id, raw_id, running):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("VOCOCO_CRON_RUN_ID", raw_id or "")
    checks.save_raw({stage: checks.parse_result(row) for stage, row in sample().items()},
                    tmp_path, (now - timedelta(minutes=1)).isoformat())
    jobs = tmp_path / "jobs.txt"
    job = {"id": "13f146ce", "last_status": "success", "last_run_at": now.timestamp(), "last_run_id": last_id}
    if running is not None:
        job["running_run_id"] = running
    jobs.write_text(json.dumps([job]))
    with pytest.raises(ValueError, match="运行中|ID"):
        daily.latest_report(tmp_path, now, jobs)


def test_daily_rejects_bad_and_missing_receipt(tmp_path):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="没有巡检回执"):
        daily.latest_report(tmp_path, now)
    (tmp_path / "2026.json").write_text("{")
    with pytest.raises(ValueError):
        daily.latest_report(tmp_path, now)
