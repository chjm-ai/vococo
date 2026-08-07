from __future__ import annotations

import asyncio
import json

from vococo.gateway.adapters.web import WebAdapter
from vococo.tools import selfops


def test_healthz_returns_process_identity(monkeypatch) -> None:
    monkeypatch.setattr(selfops, "running_revision", lambda: "abc123")
    adapter = WebAdapter()
    response = asyncio.run(adapter._handle_healthz(None))
    payload = json.loads(response.text)

    assert payload["ok"] is True
    assert payload["boot_id"] == adapter._boot_id
    assert payload["pid"] > 1
    assert payload["revision"] == "abc123"


def test_running_revision_reads_declared_state(tmp_path, monkeypatch) -> None:
    path = tmp_path / "running_revision.json"
    path.write_text('{"revision": "stable-1"}', encoding="utf-8")
    monkeypatch.setattr(selfops, "RUNNING_REVISION_PATH", path)
    assert selfops.running_revision() == "stable-1"
