"""一轮对话的执行过程时间线 —— 供落库/前端重建工具卡片用。

原本只在 gateway/core.py 的 converse() 里私有使用;2026-07-29 语音/cron/chat
三方共用 core/task_runner.py 这套后台任务引擎后,task_runner 也需要同一份录制
逻辑(否则后台任务/定时任务跑完只留一句正文,看不到过程),故提出来做公共模块。
"""
from __future__ import annotations


class Timeline:
    """把一轮事件流录成可落库的时间线:文字段与工具调用按真实顺序交错。

    结构(JSON 可序列化):
    [{"type":"text","text":...},
     {"type":"tool","name":...,"id":...,"input":{...},"ok":...,"preview":...,
      "detail":...,"subs":[{"name","ok"},...]}]
    刷新页面时 /history 带回这份时间线,前端据此原样重建工具卡与文字的交错画面。
    """

    MAX_BLOCKS = 400  # 极端长轮次的保险丝:超出只记数,不再膨胀

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self._by_id: dict[str, dict] = {}  # 顶层工具 id → block(配对 input/结果)

    def text(self, t: str) -> None:
        if self.blocks and self.blocks[-1]["type"] == "text":
            self.blocks[-1]["text"] += t
        elif len(self.blocks) < self.MAX_BLOCKS:
            self.blocks.append({"type": "text", "text": t})

    def tool_started(self, name: str, tool_id: str, parent_id: str | None) -> None:
        if parent_id:  # 子代理内部工具:挂进所属 Task 块的 subs,不占顶层块
            parent = self._by_id.get(parent_id)
            if parent is not None:
                parent.setdefault("subs", []).append({"name": name, "ok": True})
            return
        if len(self.blocks) >= self.MAX_BLOCKS:
            return
        block = {"type": "tool", "name": name, "id": tool_id, "ok": True}
        self.blocks.append(block)
        if tool_id:
            self._by_id[tool_id] = block

    def tool_input(self, tool_id: str, tool_input: dict, parent_id: str | None) -> None:
        if parent_id:
            return
        block = self._by_id.get(tool_id)
        if block is not None:
            block["input"] = tool_input

    def compacted(self, trigger: str) -> None:
        """记一个压缩标记块;前端历史重放据此显示「上下文已自动压缩」系统条。"""
        if len(self.blocks) < self.MAX_BLOCKS:
            self.blocks.append({"type": "compact", "trigger": trigger})

    def tool_finished(
        self, name: str, ok: bool, preview: str, tool_id: str,
        detail: str, parent_id: str | None,
    ) -> None:
        if parent_id:
            parent = self._by_id.get(parent_id)
            if parent is not None:  # 子步只标最后一个未完成的同名项
                for sub in reversed(parent.get("subs", [])):
                    if sub["name"] == name and "done" not in sub:
                        sub.update(done=True, ok=ok)
                        break
            return
        block = self._by_id.get(tool_id)
        if block is None:  # 没配上 id(如 hook 拦截):回退找最后一个同名未完成块
            block = next(
                (b for b in reversed(self.blocks)
                 if b["type"] == "tool" and b["name"] == name and "preview" not in b),
                None,
            )
        if block is not None:
            block.update(ok=ok, preview=preview, detail=detail)
