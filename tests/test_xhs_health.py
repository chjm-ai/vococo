"""链路校验必须按原始错误分类,不能把步骤失败猜成登录过期。"""
import datetime
import importlib.util
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
