"""消息级用量快照：缓存修正、累计输出和交错子代理不能重复记账。"""
import pytest

from vococo.core.token_usage import StreamUsage


def _start(usage):
    return {"type": "message_start", "message": {"usage": usage}}


def _delta(usage):
    return {"type": "message_delta", "usage": usage}


@pytest.mark.parametrize("start, delta, expected", [
    ({"input_tokens": 5842}, {"input_tokens": 208, "output_tokens": 5,
                            "cache_read_input_tokens": 5632}, (208, 5, 5632)),
    ({"input_tokens": 0}, {"input_tokens": 1000, "output_tokens": 200}, (1000, 200, 0)),
    ({"input_tokens": 10, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 99},
     {"output_tokens": 2}, (13, 2, 99)),
])
def test_final_snapshot_replaces_estimate(start, delta, expected):
    usage = StreamUsage()
    usage.observe(_start(start))
    usage.observe(_delta(delta))
    usage.observe({"type": "message_stop"})
    usage.finish()
    assert (usage.input_fresh, usage.output_tokens, usage.cache_read) == expected


def test_cumulative_output_counts_once_and_ignores_trailing_zero():
    usage = StreamUsage()
    usage.observe(_start({"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 1}))
    for count in [2, 5, 5, 0]:
        usage.observe(_delta({"output_tokens": count}))
    assert usage.tokens == 15


def test_parent_and_native_children_keep_separate_snapshots():
    usage = StreamUsage()
    for scope in [None, "child-a", "child-b"]:
        usage.observe(_start({"input_tokens": 100_000}), scope)
    usage.observe(_delta({"input_tokens": 10, "output_tokens": 2}), "child-a")
    usage.observe(_delta({"input_tokens": 20, "output_tokens": 3}), None)
    usage.observe(_delta({"input_tokens": 30, "output_tokens": 4}), "child-b")
    usage.observe({"type": "message_stop"}, "child-a")
    usage.finish()
    assert usage.tokens == 69


def test_unknown_input_is_deferred_but_not_lost_on_interruption():
    usage = StreamUsage()
    usage.observe(_start({"input_tokens": 25}))
    assert usage.tokens == 0
    usage.finish()
    assert usage.tokens == 25
    usage.finish()
    assert usage.tokens == 25


def test_context_is_latest_main_message_not_cumulative_or_child_usage():
    usage = StreamUsage()
    usage.observe(_start({"input_tokens": 100_000}))
    assert usage.context_tokens == 0  # 未确认的中转估计不展示。
    usage.observe(_delta({"output_tokens": 1}))
    assert usage.context_tokens == 0
    usage.observe(_delta({"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 2}))
    assert usage.context_tokens == 102
    usage.observe(_start({"input_tokens": 900, "cache_read_input_tokens": 0}), "child")
    assert usage.context_tokens == 102
    usage.observe(_start({"input_tokens": 20, "cache_read_input_tokens": 100}))
    assert usage.context_tokens == 120  # 下一次模型调用覆盖，而非跨调用累加。
    usage.observe({"type": "message_stop"})
    assert usage.context_tokens == 120


def test_each_model_call_has_its_own_snapshot():
    usage = StreamUsage()
    for _ in range(3):
        usage.observe(_start({"input_tokens": 100_000}))
        usage.observe(_delta({"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 99_990}))
    usage.finish()
    assert usage.tokens == 36
    assert usage.cache_read == 299_970
