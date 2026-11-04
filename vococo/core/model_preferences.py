"""模型参数的会话快照：全局设置只用于首次继承，不再串改已打开的会话。"""
from ..gateway import settings_store
from ..memory import session_store


def for_model(session_key: str, model: str) -> dict:
    defaults = {
        "effort": settings_store.get_web_effort(model),
        "service_tier": settings_store.get_web_service_tier(model),
    }
    if not session_key:
        return defaults
    saved = session_store.model_preferences(session_key).get(model)
    if saved is None:
        session_store.set_model_preferences(session_key, model, defaults)
        return defaults
    return {**defaults, **saved}
