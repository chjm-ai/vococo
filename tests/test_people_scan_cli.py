"""people_scan_cli.py 的信号判断测试——cron 脚本任务模式(见 cron/scheduler.py 的
_run_script_job)靠 _print_summary 的返回值决定要不要额外花一次 LLM 调用总结,
没信号(没有更新也没有待确认)就直接把这几行人话统计当结果,零 LLM 消耗。
"""
from __future__ import annotations

from vococo.memory.people_scan_cli import _print_summary


def test_no_notes_has_no_signal(capsys):
    assert _print_summary([]) is False
    assert "没有新/改动的笔记" in capsys.readouterr().out


def test_all_skipped_has_no_signal():
    summaries = [{"note": "a", "status": "skipped"}, {"note": "b", "status": "skipped"}]
    assert _print_summary(summaries) is False


def test_errors_alone_have_no_signal():
    """错误单独不算信号——原始诊断行已经够用,不值得为它调 LLM(见
    scheduler._run_script_job:ok=False 时压根不看 has_signal)。"""
    summaries = [{"note": "a", "status": "error", "reason": "boom"}]
    assert _print_summary(summaries) is False


def test_update_has_signal(capsys):
    summaries = [{"note": "a", "status": "done", "updated": ["胜源"]}]
    assert _print_summary(summaries) is True
    assert "胜源" in capsys.readouterr().out


def test_pending_alone_has_signal():
    summaries = [{"note": "a", "status": "pending"}]
    assert _print_summary(summaries) is True
