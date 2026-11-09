"""隔离服务+真实浏览器:取消、请求归属、分页和错误恢复回归。"""
import json
from pathlib import Path
import subprocess
import sys

from test_library_integration import _browser_environment


def test_fast_switch_cancels_stale_history(tmp_path):
    output = tmp_path / "history.json"
    process = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "manual/history_slow_probe.py"),
         "--tag", "regression", "--slow-ms", "1200", "--out", str(output)],
        env=_browser_environment(), capture_output=True, text=True, timeout=200,
    )
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(output.read_text())
    assert all(row["ok"] for row in result["cold"])
    assert result["burst"]["ok"] and result["burst"]["foreign"] == 0
    assert len(result["burst"]["abortedByPage"]) == 4
    assert result["burst"]["networkCancelled"] == 4
    assert result["burst"]["git"] == result["burst"]["tasks"] == 1
    assert result["aba"]["gate"][0] == {"histLoading": "probe-live", "stream": False}
    assert result["ownership"]["histLoading"] == "probe-live"
    assert result["ownership"]["stream"] is False
    assert result["abort"]["serviceState"] != "offline"
    assert result["offline"]["serviceState"] == "offline"
    assert result["offline"]["histLoading"] is None
    assert result["offline"]["skeleton"] is False
    assert result["pending"]["ok"] and result["pending"]["draftBubbles"] == 1
    assert result["earlier"]["before"] == 40 and result["earlier"]["after"] == 60
    assert result["earlier"]["hasMore"] is False
    assert result["hot"]["ok"] and result["anchor"]["found"]
    assert result["permission"] == {"cleared": True, "conv": "perm-b", "mode": "standard"}
    assert result["cachedSwitch"] == {"wasLoading": True, "skeleton": False, "oldGit": False}
    assert result["errors"] == []
    assert result["server"]["cancelled"] >= 4
