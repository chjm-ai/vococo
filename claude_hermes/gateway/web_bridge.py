"""语音 → 网页跨端续聊的桥接层。

语音模块(voice/task_tools.py)不持有 GatewayRunner/WebAdapter 实例——同一进程内
两条线路本来就是分开跑的(voice 路由直接挂在 aiohttp app 上,不走 GatewayRunner._dispatch,
见 gateway/run.py 里"语音模式不走 GatewayRunner"那条注释)。这里用运行时注册回调的方式
接起来,跟 voice/notify.py 的 register_platform_push 是同一套模式,避免 voice ↔ gateway
之间形成模块级循环 import。

纯语音模式(没有起 GatewayRunner/没开网页入口)时没人调 register(),available() 为 False,
调用方(voice_continue_session)据此告诉用户"当前没有网页入口"而不是报一个费解的异常。
"""
from __future__ import annotations

from typing import Awaitable, Callable

_dispatch_fn: Callable[[str, str], Awaitable[None]] | None = None
_cancel_fn: Callable[[str], bool] | None = None


def register(
    dispatch_fn: Callable[[str, str], Awaitable[None]],
    cancel_fn: Callable[[str], bool],
) -> None:
    """由 GatewayRunner.run() 在起好 WebAdapter 后调用一次。"""
    global _dispatch_fn, _cancel_fn
    _dispatch_fn = dispatch_fn
    _cancel_fn = cancel_fn


def available() -> bool:
    return _dispatch_fn is not None


def cancel_if_running(session_key: str) -> bool:
    """该网页会话若正在跑,打断它;返回是否真的打断了(没在跑/没注册都是 False)。

    web 会话没有后台任务那种常驻 asyncio.Task,"正在跑"只在一轮 converse()
    执行期间以 GatewayRunner._cancel_scopes[session_key] 的形式短暂存在——打断后
    per-session 锁会在当前这轮收尾后自然释放,继续 dispatch 的新一轮会自动排在
    它后面执行,不需要额外等待/重试(见 GatewayRunner._dispatch 的 lock 语义)。
    """
    return bool(_cancel_fn and _cancel_fn(session_key))


async def continue_session(conv: str, text: str) -> None:
    """往 web:<conv> 会话注入一条消息,走跟浏览器发送完全相同的处理流水线
    (标题占位/项目 touch/用户气泡广播/入队 dispatch,见 WebAdapter.inject)。
    未注册(纯语音模式)时静默跳过——调用方应先用 available() 判断。"""
    if _dispatch_fn is not None:
        await _dispatch_fn(conv, text)
