"""实例自己的配置，保存在不进入 Git 的 data/instance_config.json。"""
from __future__ import annotations

import json
from pathlib import Path


def section(name: str, *, path: Path | None = None) -> dict:
    # 独立脚本可指定配置路径，不加载主程序的登录配置。
    if path is None:
        from . import config

        path = config.DATA_DIR / "instance_config.json"
    try:
        with path.open(encoding="utf-8") as stream:
            raw = stream.read(256_001)
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise ValueError("instance_config.json 必须是 UTF-8 文本") from exc
    if len(raw) > 256_000:
        raise ValueError("实例配置超过 256000 字符")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("instance_config.json 不是有效 JSON") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("实例配置必须是 version=1 的对象")
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"实例配置 {name} 必须是对象")
    return value


def text(name: str, key: str, default: str = "") -> str:
    value = section(name).get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"实例配置 {name}.{key} 必须是文本")
    return value


def strings(name: str, key: str) -> list[str]:
    value = section(name).get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"实例配置 {name}.{key} 必须是非空文本列表")
    return value
