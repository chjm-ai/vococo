"""链路校验必须按原始错误分类,不能把步骤失败猜成登录过期。"""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "xhs_health", Path(__file__).resolve().parents[1] / "scripts/xhs_health.py"
)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class HealthTests(unittest.TestCase):
    def test_failure_categories_preserve_original_error(self):
        cases = [
            (["配置错误：账号不存在：default。请先执行 add-account 注册。",
              "FAIL: check-login failed or timed out (exit=2, limit=90s)"], "账号配置错误"),
            (["Error: CDP 端口 9333 已被未知或其他账号的 Chrome 占用，不能安全复用为账号 linxai。",
              "FAIL: check-login failed or timed out (exit=1, limit=90s)"], "端口被未知或其他账号占用"),
            (["FAIL: xhs not logged in"], "小红书未登录"),
            (["FAIL: chrome cdp still unhealthy after safe launch attempt"], "Chrome CDP 连接异常"),
            (["FAIL: cannot get exit IP after 3 tries (network down?)"], "网络出口检测失败"),
            (["FAIL at stage: timeout(1200s)"], "整轮爬虫超过运行时限"),
            (["FAIL: check-login failed or timed out (exit=143, limit=90s)"], "预检步骤被终止或超时"),
            (["FAIL: check-login failed or timed out (exit=137, limit=90s)"], "预检步骤被强制终止"),
            (["FAIL: check-login failed or timed out (exit=1, limit=90s)"], "登录检查执行失败(原因未确定)"),
            (["FAIL: smoke search-feeds failed (exit=1)"], "搜索冒烟测试失败(原因未确定)"),
            (["FAIL: captcha", "FAIL: check-login failed (exit=1)"], "触发了验证码"),
            (["FAIL: 安全中心", "FAIL: check-login failed (exit=1)"], "触发了小红书风控"),
            (["FAIL: unexpected error"], "unexpected error"),
        ]
        for lines, expected in cases:
            with self.subTest(expected=expected):
                result = health.crawler_failure(lines)
                self.assertIn(expected, result)
                self.assertIn(lines[0], result)
                self.assertNotIn("页面卡死或小红书登录过期", result)

    def crawler_with_log(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            (root / "logs/daily.log").write_text("\n".join(lines), encoding="utf-8")
            with patch.object(health, "CRAWL", root), \
                    patch.object(health, "today", "2026-10-13"), \
                    patch.object(health, "now", datetime.datetime(2026, 10, 13, 13, 30)):
                return health.crawler()

    def test_latest_attempt_does_not_reuse_old_error(self):
        _, bad, _ = self.crawler_with_log([
            "[2026-10-13T08:30:00] === daily.sh start ===",
            "[2026-10-13T08:30:00] 配置错误：账号不存在：default",
            "[2026-10-13T12:45:00] === daily.sh start ===",
            "[2026-10-13T13:10:00] FAIL at stage: timeout(1500s)",
        ])
        self.assertIn("整轮爬虫超过运行时限", bad[0])
        self.assertNotIn("账号配置错误", bad[0])

    def test_old_success_does_not_hide_latest_failure(self):
        _, bad, done = self.crawler_with_log([
            "[2026-10-13T08:30:00] === daily.sh start ===",
            "[2026-10-13T08:45:00] === daily.sh done ===",
            "[2026-10-13T12:45:00] === daily.sh start ===",
            "[2026-10-13T12:46:00] FAIL: xhs not logged in",
        ])
        self.assertIsNone(done)
        self.assertIn("小红书未登录", bad[0])

    def test_successful_retry_clears_old_failure(self):
        ok, bad, done = self.crawler_with_log([
            "[2026-10-13T08:30:00] === daily.sh start ===",
            "[2026-10-13T08:30:00] FAIL: check-login failed (exit=2)",
            "[2026-10-13T12:45:00] === daily.sh start ===",
            "[2026-10-13T13:00:00] === daily.sh done ===",
        ])
        self.assertEqual(bad, [])
        self.assertEqual(ok, ["爬虫 13:00 跑完"])
        self.assertEqual(done, datetime.datetime(2026, 10, 13, 13))

    def test_empty_log_reports_no_run(self):
        _, bad, done = self.crawler_with_log([])
        self.assertIsNone(done)
        self.assertIn("爬虫今天没开跑", bad[0])


@pytest.fixture
def configured(tmp_path, monkeypatch):
    crawl = tmp_path / "custom-crawl"
    (crawl / "logs").mkdir(parents=True)
    clock = datetime.datetime(2026, 10, 13, 13, 30)
    monkeypatch.setattr(health, "now", clock)
    monkeypatch.setattr(health, "today", "2026-10-13")
    jobs = tmp_path / "custom-jobs.json"
    jobs.write_text(json.dumps([
        {"id": "unrelated", "enabled": False, "last_status": "error"},
        {"id": "custom-publish", "enabled": True, "last_status": "ok", "last_run_at": clock.timestamp()},
    ]))
    config = tmp_path / "instance_config.json"
    values = {"version": 1, "xhs_health": {
        "crawl_dir": str(crawl), "cron_jobs_path": str(jobs), "publish_job_id": "custom-publish",
        "publish_log_path": "~/remote-example/publish.log",
    }}
    config.write_text(json.dumps(values))
    monkeypatch.setenv("VOCOCO_INSTANCE_CONFIG", str(config))
    health.configure()
    (crawl / "logs/daily.log").write_text("[2026-10-13T13:00:00] === daily.sh done ===\n")
    marker = crawl / "data/daily_analysis/2026-10-13/analysis.completed"
    marker.parent.mkdir(parents=True)
    marker.touch()
    return config, values, crawl, jobs, marker


def test_configured_full_main_reads_custom_targets_without_writing(configured, capsys):
    config, values, crawl, jobs, marker = configured
    before = {p: p.read_bytes() for p in config.parent.rglob("*") if p.is_file()}
    health.main()
    out = capsys.readouterr().out
    assert "爬虫 13:00 跑完" in out and "分析已出" in out and "发布正常" in out
    assert "##CRON_SIGNAL:0##" in out
    assert before == {p: p.read_bytes() for p in config.parent.rglob("*") if p.is_file()}
    assert health.CRAWL == crawl and health.CRON_JOBS == jobs
    assert health.PUBLISH_JOB == "custom-publish"


@pytest.mark.parametrize("state,expected", [
    ("disabled", "发布任务被停用了"), ("error", "~/remote-example/publish.log"),
    ("missing", "读不到发布任务 custom-publish"), ("broken", "读不到发布任务"),
    ("old", "发布今天没轮到"),
])
def test_publish_status_uses_only_configured_job(configured, state, expected):
    jobs = configured[3]
    values = json.loads(jobs.read_text())
    if state == "disabled":
        values[1]["enabled"] = False
    elif state == "error":
        values[1]["last_status"] = "error"
    elif state == "missing":
        values.pop()
    elif state == "old":
        values[1]["last_run_at"] -= 27 * 3600
    jobs.write_text("{" if state == "broken" else json.dumps(values))
    ok, bad = health.publish()
    assert expected in " ".join(ok + bad)
    assert bool(bad) == (state != "old")


def test_crawler_and_analysis_keep_waiting_thresholds(configured):
    crawl, marker = configured[2], configured[4]
    marker.unlink()
    assert health.analysis(health.now - datetime.timedelta(minutes=44)) == (["分析排队中"], [])
    assert health.analysis(health.now - datetime.timedelta(minutes=45))[1]
    (crawl / "logs/daily.log").write_text("")
    assert str(crawl / "scripts/daily.sh") in health.crawler()[1][0]
    (crawl / "logs/daily.log").unlink()
    assert "爬虫日志不存在" in health.crawler()[1][0]


@pytest.mark.parametrize("key,value", [
    ("crawl_dir", "relative"), ("crawl_dir", []), ("cron_jobs_path", ""),
    ("publish_job_id", ""), ("publish_job_id", None), ("publish_job_id", "wrong id"),
    ("publish_log_path", ""), ("publish_log_path", "line\nbreak"),
])
def test_invalid_config_stops_before_checking_targets(configured, monkeypatch, capsys, key, value):
    config, values = configured[:2]
    values["xhs_health"][key] = value
    config.write_text(json.dumps(values))
    monkeypatch.setattr(health, "crawler", lambda: pytest.fail("坏配置不能检查业务目录"))
    monkeypatch.setattr(health, "publish", lambda: pytest.fail("坏配置不能读取任务文件"))
    with pytest.raises(SystemExit) as exc:
        health.main()
    assert exc.value.code == 1
    assert "##CRON_SIGNAL:1##" in capsys.readouterr().out


@pytest.mark.parametrize("raw", [None, "{", '{"version":1}', '{"version":1,"xhs_health":[]}'])
def test_missing_or_bad_config_cli_needs_no_login_and_writes_nothing(tmp_path, raw):
    config = tmp_path / "instance_config.json"
    if raw is not None:
        config.write_text(raw)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    env = dict(os.environ, VOCOCO_INSTANCE_CONFIG=str(config))
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    result = subprocess.run([sys.executable, str(Path(health.__file__))], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1 and not result.stderr
    assert "配置不可用" in result.stdout and "##CRON_SIGNAL:1##" in result.stdout
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_xhs_migration_extracts_only_old_literals_and_preserves_other_sections(tmp_path, monkeypatch):
    path = Path(health.__file__).parent / "migrate_local_preferences.py"
    spec = importlib.util.spec_from_file_location("xhs_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    source = '''HOME = pathlib.Path.home()
CRAWL = HOME / "custom/crawl"
CRON_JOBS = HOME / "custom/cron.json"
PUBLISH_JOB = "example-job"
MESSAGE = "队列发布上一轮失败:先看远程机 ~/custom/publish.log(失败项不会隔天自动补发)"
raise RuntimeError("不能执行旧代码")
'''
    monkeypatch.setattr(migration, "source", lambda ref, name: source)
    monkeypatch.setattr(migration.Path, "home", lambda: tmp_path)
    values = migration.extract_xhs_health("old-ref")
    assert values == {"xhs_health": {
        "crawl_dir": str(tmp_path / "custom/crawl"), "cron_jobs_path": str(tmp_path / "custom/cron.json"),
        "publish_job_id": "example-job", "publish_log_path": "~/custom/publish.log",
    }}
    config = tmp_path / "instance_config.json"
    original = {"version": 1, "upwork": {"cdp_port": 12345}, "brain": {"agent_id": "unchanged"}}
    config.write_text(json.dumps(original))
    backup = migration.append_config(config, values, apply=True)
    assert json.loads(backup.read_text()) == original
    assert json.loads(config.read_text()) == original | values
    health.configure(config)
    assert health.PUBLISH_JOB == "example-job"
    monkeypatch.setattr(migration, "source", lambda ref, name: source.replace('HOME / "custom/crawl"', 'get_path()'))
    with pytest.raises(ValueError, match="表达式变化"):
        migration.extract_xhs_health("old-ref")


if __name__ == "__main__":
    unittest.main()
