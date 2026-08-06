"""send_image 工具中途发图(mid_turn)不该被当成"回合结束"处理。

背景:_track_live 原来把任意 type="message" 事件当成一轮的收尾(pop 掉 _live[conv]
快照)。但 send_image 是在回合进行中途触发的,后面还会有更多正文/工具事件——
如果收了轮,断线重连的客户端就拿不到这之后的内容。mid_turn=True 标记让它像
tool_start/text 一样只追加成一帧,不收轮。
"""
from __future__ import annotations

from vococo.gateway.adapters.web import WebAdapter


def test_mid_turn_message_does_not_close_live_turn():
    a = WebAdapter()
    a._emit({"conv": "1", "type": "start"})
    assert "1" in a._live

    a._emit({
        "conv": "1", "type": "message", "text": "发个图",
        "images": ["/image?name=x.png"], "mid_turn": True,
    })
    assert "1" in a._live  # 没被收轮
    frame_types = [p.get("type") for _, p in a._live["1"]["frames"]]
    assert frame_types == ["start", "message"]  # 图片帧按序追加,不是替换/丢弃

    # 回合真正结束时(没有 mid_turn)才收轮
    a._emit({"conv": "1", "type": "message", "text": "最终回复"})
    assert "1" not in a._live


def test_normal_message_still_closes_live_turn():
    """非 mid_turn 的 message(命令回复/报错/cron 推送)行为不变:直接收轮。"""
    a = WebAdapter()
    a._emit({"conv": "1", "type": "start"})
    a._emit({"conv": "1", "type": "message", "text": "出错了"})
    assert "1" not in a._live
