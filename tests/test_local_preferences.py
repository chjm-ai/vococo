"""个人配置迁移与离线回归：不启动 launchd/Chrome，不访问网站。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vococo import config
from vococo.core import proxy_health
from vococo.cron import system_tasks

ROOT = Path(__file__).parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    path = tmp_path / "instance_config.json"
    values = {
        "version": 1,
        "local_services": {"proxy_launchd_label": "org.example.proxy", "display_names": {"org.example.job": "测试任务"}},
        "upwork": {
            "cdp_port": 12345, "chrome_bin": "/example/Chrome", "chrome_profile": str(tmp_path / "profile"),
            "output_dir": str(tmp_path / "output"), "search_queries": ["test first", "test second"],
            "match_keywords": ["automation", "agent", "workflow", "automation"],
        },
    }
    path.write_text(json.dumps(values))
    return path, values


def test_explicit_path_does_not_import_runtime_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"version":1,"upwork":{"cdp_port":12345}}')
    code = (
        "import sys; from pathlib import Path; from vococo.instance_config import section; "
        f"assert section('upwork', path=Path({str(path)!r}))['cdp_port'] == 12345; "
        "assert 'vococo.config' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)


def test_custom_display_names_and_fallback(private):
    assert system_tasks._display_names().get("org.example.job") == "测试任务"
    assert system_tasks._display_names().get("unknown", "unknown") == "unknown"
    private[0].unlink()
    assert system_tasks._display_names() == {}


@pytest.mark.parametrize("names", [[], "wrong", {"a": 2}, {"": "测试"}, {"a": " "}])
def test_bad_display_names_rejected(private, names):
    path, values = private
    values["local_services"]["display_names"] = names
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="display_names"):
        system_tasks._display_names()


def test_configured_service_is_the_only_spawn_target(private, monkeypatch):
    calls = []

    async def communicate():
        return b"", None

    async def spawn(*args, **kwargs):
        calls.append(args)
        # spawn 已取得快照，后续配置变化不能改变本次目标。
        private[0].write_text('{"version":1,"local_services":{"proxy_launchd_label":"org.other.proxy"}}')
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(proxy_health.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(proxy_health._kickstart()) == (True, "")
    assert calls == [("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/org.example.proxy")]


@pytest.mark.parametrize("label", [None, "", "-k", "gui/999/org.other", "a b", "a\n", 23, "a" * 256])
def test_invalid_service_never_spawns(private, monkeypatch, label):
    path, values = private
    values["local_services"]["proxy_launchd_label"] = label
    path.write_text(json.dumps(values))

    async def forbidden(*args, **kwargs):
        pytest.fail("坏配置不应运行 launchctl")

    monkeypatch.setattr(proxy_health.asyncio, "create_subprocess_exec", forbidden)
    assert asyncio.run(proxy_health._kickstart())[0] is False


def test_missing_config_restart_failure_does_not_update_state(private, monkeypatch):
    private[0].unlink()
    monkeypatch.setattr(proxy_health, "_STATE_PATH", private[0].parent / "state.json")
    monkeypatch.setattr(proxy_health, "_LOG_PATH", private[0].parent / "health.log")

    async def diagnose():
        return dict(configured=True, ok=False, answered=False, want=["test-model"], listed=None,
                    missing=["test-model"], code=0, detail="offline", base_url="http://localhost:12345")

    async def forbidden(*args, **kwargs):
        pytest.fail("缺配置不应运行 launchctl")

    monkeypatch.setattr(proxy_health, "_diagnose", diagnose)
    monkeypatch.setattr(proxy_health.asyncio, "create_subprocess_exec", forbidden)
    result = asyncio.run(proxy_health.check())
    assert result["action"] == "restart-failed" and result["alert"]
    assert not proxy_health._STATE_PATH.exists()


def test_upwork_config_preserves_order_scoring_and_seen(private):
    module = script("upwork-scraper")
    module.configure(private[0])
    assert module.SEARCH_QUERIES == private[1]["upwork"]["search_queries"]
    assert module.MATCH_KEYWORDS == private[1]["upwork"]["match_keywords"]
    assert module.CDP_PORT == 12345
    assert module.SEEN_FILE == Path(private[1]["upwork"]["output_dir"]) / "seen_ids.json"
    assert module.match_stars(dict(title="AUTOMATION AGENT WORKFLOW", description="", skills=[])) == 3
    assert module.match_stars(dict(title="automation", description="", skills=[])) == 2
    module.OUTPUT_DIR.mkdir()
    module.SEEN_FILE.write_text('{"old-id":"2099-01-01"}')
    assert module.load_seen() == {"old-id": "2099-01-01"}
    module.save_seen({"old-id": "2099-01-01", "expired": "2000-01-01"})
    assert module.load_seen() == {"old-id": "2099-01-01"}


@pytest.mark.parametrize("key,value", [
    ("cdp_port", True), ("cdp_port", 0), ("cdp_port", 65536), ("cdp_port", "12345"),
    ("chrome_bin", "relative"), ("chrome_profile", ""), ("output_dir", []),
    ("search_queries", "wrong"), ("match_keywords", [3]), ("match_keywords", [" "]),
])
def test_upwork_bad_config_before_any_browser_or_file(private, monkeypatch, capsys, key, value):
    path, values = private
    values["upwork"][key] = value
    path.write_text(json.dumps(values))
    monkeypatch.setenv("VOCOCO_INSTANCE_CONFIG", str(path))
    module = script("upwork-scraper")
    monkeypatch.setattr(module, "ensure_chrome", lambda _: pytest.fail("不应启动浏览器"))
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert "##CRON_SIGNAL:1##" in capsys.readouterr().out
    assert not (path.parent / "output").exists()


def test_upwork_chrome_uses_configured_arguments(private, monkeypatch):
    module = script("upwork-scraper")
    module.configure(private[0])
    answers = iter([None, "ws://localhost/fake"])
    calls = []
    monkeypatch.setattr(module, "get_ws_url", lambda port: next(answers))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert module.ensure_chrome(module.CDP_PORT) == "ws://localhost/fake"
    assert calls == [["/example/Chrome", "--remote-debugging-port=12345",
                      f"--user-data-dir={private[1]['upwork']['chrome_profile']}", "--no-first-run"]]


@pytest.mark.parametrize("scenario", ["failed", "partial", "unchanged", "new"])
def test_upwork_main_fake_pages_preserve_baseline(private, monkeypatch, capsys, scenario):
    path, values = private
    monkeypatch.setenv("VOCOCO_INSTANCE_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["upwork-scraper.py", "--search"])
    module = script("upwork-scraper")
    output = Path(values["upwork"]["output_dir"])
    output.mkdir()
    seen = output / "seen_ids.json"
    seen.write_text('{"existing":"2099-01-01"}')
    before = seen.read_bytes()
    page = SimpleNamespace(close=lambda: None)
    browser = SimpleNamespace(contexts=[SimpleNamespace(new_page=lambda: page)])
    engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: engine)))
    monkeypatch.setattr(module, "ensure_chrome", lambda _: "ws://fake")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    urls = []
    job = dict(id="existing", title="AUTOMATION", description="", skills=[], budget="", tier="", proposals="",
               client_spent="", client_country="", payment_verified=True, url="", posted="", duration="")

    def scrape(page, url):
        urls.append(url)
        if scenario == "failed" or (scenario == "partial" and "search/jobs" in url):
            return "BLOCKED", []
        return "OK", [dict(job)] + ([dict(job, id="new-id")] if scenario == "new" else [])

    monkeypatch.setattr(module, "scrape_url", scrape)
    if scenario == "failed":
        with pytest.raises(SystemExit):
            module.main()
        assert seen.read_bytes() == before
        assert list(output.iterdir()) == [seen]
    else:
        module.main()
        data = json.loads(next(output.glob("jobs_*.json")).read_text())
        assert data["new_count"] == int(scenario == "new")
        assert data["total_unique"] == (2 if scenario == "new" else 1)
        assert data["status"] == ("PARTIAL" if scenario == "partial" else "OK")
        assert data["jobs"][0]["stars"] == 2
        markdown = next(output.glob("jobs_*.md")).read_text()
        assert markdown.count("### ") == int(scenario == "new")
    # 搜索拦一次后，不继续访问第二个搜索词；正常时保持搜索词顺序。
    assert len(urls) == (3 if scenario in ("failed", "partial") else 4)
    assert "q=test%20first" in urls[2]
    if len(urls) == 4:
        assert "q=test%20second" in urls[3]
    signal = 0 if scenario == "unchanged" else 1
    assert f"##CRON_SIGNAL:{signal}##" in capsys.readouterr().out


def test_upwork_missing_config_without_login_or_playwright(tmp_path):
    env = dict(os.environ, VOCOCO_INSTANCE_CONFIG=str(tmp_path / "missing.json"))
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/upwork-scraper.py")], env=env,
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 1 and "配置不可用" in result.stdout
    assert "##CRON_SIGNAL:1##" in result.stdout and not result.stderr
    assert not list(tmp_path.iterdir())


def test_extract_old_values_without_executing_sources(monkeypatch, tmp_path):
    migration = script("migrate_local_preferences")
    sources = {
        "system_tasks.py": '_DISPLAY_NAMES: dict[str,str] = {"org.example.job":"测试任务"}',
        "proxy_health.py": 'SERVICE = f"gui/{os.getuid()}/org.example.proxy"',
        "upwork-scraper.py": '''CDP_PORT = 12345
CHROME_BIN = "/example/Chrome"
CHROME_PROFILE = Path.home() / ".example/profile"
OUTPUT_DIR = Path.home() / "example/output"
SEARCH_QUERIES = ["second", "first"]
MATCH_KEYWORDS = ["agent", "agent"]
raise RuntimeError("旧代码不得执行")
''',
    }
    monkeypatch.setattr(migration, "source", lambda ref, name: sources[Path(name).name])
    monkeypatch.setattr(migration.Path, "home", lambda: tmp_path)
    data = migration.extract("old-ref")
    assert data["local_services"]["proxy_launchd_label"] == "org.example.proxy"
    assert data["upwork"]["output_dir"] == str(tmp_path / "example/output")
    assert data["upwork"]["search_queries"] == ["second", "first"]
    assert data["upwork"]["match_keywords"] == ["agent", "agent"]
    with pytest.raises(ValueError):
        migration.home_path(__import__("ast").parse('evil()', mode="eval").body)


def test_append_migration_backup_preserves_unrelated_data(private):
    migration = script("migrate_local_preferences")
    path, values = private
    original = {"version": 1, "brain": {"agent_id": "unchanged"}, "custom": [1, 2]}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    additions = {key: values[key] for key in ("local_services", "upwork")}
    assert migration.append_config(path, additions) is None
    assert path.read_bytes() == before and not list(path.parent.glob("*.backup-*"))
    backup = migration.append_config(path, additions, apply=True)
    assert backup.read_bytes() == before
    assert json.loads(path.read_text()) == original | additions
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert migration.append_config(path, additions, apply=True) is None
    assert len(list(path.parent.glob("*.backup-*"))) == 1


def test_append_conflict_bad_config_and_symlink_rejected(private):
    migration = script("migrate_local_preferences")
    path, _ = private
    before = path.read_bytes()
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.append_config(path, {"upwork": {"cdp_port": 54321}}, apply=True)
    assert path.read_bytes() == before and not list(path.parent.glob("*.backup-*"))
    link = path.parent / "alias.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="软链接"):
        migration.append_config(link, {}, apply=True)
    path.write_text('{"version":2}')
    with pytest.raises(ValueError, match="version"):
        migration.append_config(path, {}, apply=True)
