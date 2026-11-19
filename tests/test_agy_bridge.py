import asyncio
import json
import os
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from scripts.agy_bridge import DEFAULT_MODEL, make_app

KEY = "test-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}
FAKE_CLI = r'''
import json, os, subprocess, sys, time
from pathlib import Path

model = sys.argv[sys.argv.index('--model') + 1]
msg = json.loads(sys.stdin.readline())
assert msg['event'] == 'user'
assert 'AGY_BRIDGE_TOKEN' not in os.environ
prompt = msg['message']['content']
assert prompt not in sys.argv

def send(event, **data):
    print(json.dumps(dict(event=event, **data)), flush=True)

send('init', init={'model': model, 'cwd': '/wrong' if prompt == 'wrong-cwd' else os.getcwd()})
if prompt == 'broken':
    print('not NDJSON', flush=True)
    sys.exit(0)
send('step_update', step_update={'step_type': 'thought', 'text_delta': '不该显示'})
send('step_update', step_update={'step_type': 'agent_response', 'text_delta': '前半'})
if prompt == 'hang':
    Path('pid').write_text(str(os.getpid()))
    time.sleep(60)
if prompt == 'orphan':
    child = r"""
import signal, time
from pathlib import Path
def done(sig, frame):
    Path('child-ended').touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, done)
Path('child-ready').touch()
time.sleep(60)
"""
    subprocess.Popen([sys.executable, '-c', child])
    while not Path('child-ready').exists():
        time.sleep(0.02)
    sys.exit(0)
if prompt == 'fail':
    send('result', result={'status': 'ERROR', 'error': 'SECRET_UPSTREAM_ERROR'})
    sys.exit(1)
if prompt == 'abrupt':
    sys.exit(0)
send('result', result={'status': 'SUCCESS', 'response': '对不上' if prompt == 'mismatch' else '前半后半'})
'''


@pytest.fixture
def cli(tmp_path):
    file = tmp_path / "fake.py"
    file.write_text(FAKE_CLI, encoding="utf-8")
    return (sys.executable, str(file)), tmp_path


def client_for(cli, timeout=5):
    command, cwd = cli
    return TestClient(TestServer(make_app(KEY, command, cwd, timeout), handler_cancellation=True))


def post(client, prompt, stream=False):
    return client.post("/v1/generate", headers=HEADERS, json={"prompt": prompt, "stream": stream})


def test_json_and_sse_have_same_text(cli):
    async def run():
        async with client_for(cli) as client:
            async with await post(client, "ok") as response:
                assert response.status == 200
                assert await response.json() == {"model": DEFAULT_MODEL, "text": "前半后半"}
            async with await post(client, "ok", True) as response:
                assert response.content_type == "text/event-stream"
                events = (await response.text()).split("\n\n")
                assert events[0] == ": connected"
                records = [(lines[0][7:], json.loads(lines[1][6:]))
                           for event in events[1:-1] if (lines := event.splitlines())]
                assert records == [("delta", {"text": "前半"}), ("delta", {"text": "后半"}),
                                   ("done", {"model": DEFAULT_MODEL})]

    asyncio.run(run())


@pytest.mark.parametrize(("prompt", "code"), [
    ("fail", "agy_request_failed"), ("abrupt", "agy_no_result"),
    ("broken", "agy_protocol_error"), ("mismatch", "agy_stream_mismatch"),
    ("wrong-cwd", "agy_workspace_mismatch"),
])
def test_upstream_error_is_not_a_success_or_leak(cli, prompt, code):
    async def run():
        async with client_for(cli) as client:
            async with await post(client, prompt) as response:
                assert response.status == 502
                assert await response.json() == {"error": code}
            async with await post(client, prompt, True) as response:
                body = await response.text()
                assert f'event: error\ndata: {{"code": "{code}"}}' in body
                assert "event: done" not in body and "SECRET_UPSTREAM_ERROR" not in body

    asyncio.run(run())


def test_invalid_request_and_auth(cli):
    async def run():
        async with client_for(cli) as client:
            async with await client.post("/v1/generate", json={"prompt": "hi"}) as response:
                assert response.status == 401
            for payload in ({"prompt": " "}, {"prompt": "hi", "model": "other"},
                            {"prompt": "x" * 8001}, {"prompt": "hi", "stream": "true"},
                            {"prompt": "hi", "tools": []}):
                async with await client.post("/v1/generate", headers=HEADERS, json=payload) as response:
                    assert response.status == 400
            async with await client.post("/v1/generate", headers=HEADERS, data="bad") as response:
                assert response.status == 415

    asyncio.run(run())


def assert_reaped(pid_file):
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.getpgid(int(pid_file.read_text()))


def test_timeout_reaps_child(cli):
    async def run():
        async with client_for(cli, timeout=1) as client:
            async with await post(client, "hang") as response:
                assert response.status == 504
                assert await response.json() == {"error": "agy_timeout"}
            assert_reaped(cli[1] / "pid")

    asyncio.run(run())


def test_disconnect_reaps_child_and_releases_slot(cli):
    async def run():
        async with client_for(cli, timeout=15) as client:
            async with await post(client, "hang", True) as response:
                async with asyncio.timeout(3):
                    assert await response.content.readuntil(b"event: delta\n")
                async with await post(client, "ok") as busy:
                    assert busy.status == 429
                response.close()
            async with asyncio.timeout(3):
                while True:
                    async with await post(client, "ok") as retry:
                        if retry.status != 429:
                            assert retry.status == 200
                            break
                    await asyncio.sleep(0.05)
            assert_reaped(cli[1] / "pid")

    asyncio.run(run())


def test_exited_cli_parent_does_not_leave_a_child_holding_stdout(cli):
    async def run():
        async with client_for(cli, timeout=1) as client:
            async with await post(client, "orphan") as response:
                assert response.status == 504
                assert (cli[1] / "child-ended").exists()

    asyncio.run(run())
