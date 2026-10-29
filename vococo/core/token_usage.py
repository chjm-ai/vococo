"""按消息合并流式用量快照，不把中转的输入估计和累计输出重复计费。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Snapshot:
    usage: dict = field(default_factory=dict)
    confirmed: bool = False
    counted: tuple[int, int, int] = (0, 0, 0)
    context_confirmed: bool = False


@dataclass
class StreamUsage:
    input_fresh: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    context_tokens: int = 0
    _active: dict[str, _Snapshot] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input_fresh + self.output_tokens

    def observe(self, event: dict, parent_id: str | None = None) -> None:
        scope = parent_id or ""
        kind = event.get("type")
        if kind == "message_start":
            self._finish(scope)
            usage = (event.get("message") or {}).get("usage") or {}
            # Codex 中转的开头只有整段输入估计，缓存明细在结尾才返回。
            confirmed = "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
            self._active[scope] = _Snapshot(dict(usage), confirmed, context_confirmed=confirmed)
            self._update(self._active[scope])
        elif kind == "message_delta":
            snapshot = self._active.setdefault(scope, _Snapshot())
            delta = event.get("usage") or {}
            snapshot.usage.update(delta)
            snapshot.context_confirmed |= "input_tokens" in delta
            snapshot.confirmed = True
            self._update(snapshot)
        elif kind == "message_stop":
            self._finish(scope)
        snapshot = self._active.get(scope)
        if not scope and snapshot and snapshot.context_confirmed and "input_tokens" in snapshot.usage:
            # 只取主模型最近一次请求，不把跨工具调用吞吐或子代理窗口相加。
            self.context_tokens = sum(snapshot.counted)

    def finish(self) -> None:
        """流被中断、没有最终用量时，保守记录尚未确认的输入估计。"""
        for scope in list(self._active):
            self._finish(scope)

    def _finish(self, scope: str) -> None:
        snapshot = self._active.pop(scope, None)
        if snapshot is not None:
            snapshot.confirmed = True
            self._update(snapshot)

    def _update(self, snapshot: _Snapshot) -> None:
        usage = snapshot.usage
        fresh = (int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                 if snapshot.confirmed else 0)
        output = max(snapshot.counted[1], int(usage.get("output_tokens") or 0))
        cached = int(usage.get("cache_read_input_tokens") or 0)
        before = snapshot.counted
        self.input_fresh += fresh - before[0]
        self.output_tokens += output - before[1]
        self.cache_read += cached - before[2]
        snapshot.counted = fresh, output, cached
