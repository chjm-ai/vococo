"""Web Push 通知授权失败的前端回归测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

SETTINGS_JS = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/settings.js"


@pytest.fixture(scope="module")
def source() -> str:
    return SETTINGS_JS.read_text(encoding="utf-8")


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def test_permission_denied_message_only_uses_ios_recovery_for_ios(source: str) -> None:
    """macOS 拒绝授权不能提示 iOS 专属的删主屏修复方式。"""
    helper = _slice(source, "function permissionDeniedMessage()", "function renderNotifyPane()")
    enable = _slice(source, "async function enableNotify()", "async function disableNotify()")

    assert "isIOS()" in helper
    assert "iOS 需删掉主屏图标" in helper
    assert "浏览器或系统设置" in helper
    assert 'throw new Error(permissionDeniedMessage())' in enable
    assert "iOS 需删掉主屏图标" not in enable


def test_denied_permission_keeps_toggle_clickable(source: str) -> None:
    """已拒绝时，开关仍应可点击以展示恢复通知权限的指引。"""
    bind = _slice(source, "async function bindNotifyPane()", "// ── Web Push 底座")

    assert "if(denied){ tgl.checked=false; tgl.disabled=false;" in bind
