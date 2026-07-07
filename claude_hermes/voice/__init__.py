"""语音伴聊模式(实验性,见 docs/design/voice-companion/)。

对外唯一入口是 register_routes(app);其余一切实现细节都在本包内,
删掉整个 claude_hermes/voice/ 目录即可彻底移除本功能(见 00-overview.md §2.4)。
"""
from __future__ import annotations

from .routes import register_routes

__all__ = ["register_routes"]
