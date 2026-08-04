"""把旧 cc-switch 配置文件(~/.claude-hermes/config.yaml)里的供应商导入到
vococo 自己的 settings_store(web_providers)中。

用法:
  python -m vococo.gateway.migrate_cc_switch [--dry-run]

--dry-run:只打印会导入什么,不写入 web_settings.json。

注意:此脚本只负责把 cc-switch 的数据搬到我们自己的存储。搬完后 vococo
不再需要 cc-switch 运行时同步——改供应商直接走设置页即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import providers
from ..gateway import settings_store


def _load_yaml() -> dict:
    """从 cc_switch_config_path() 读 YAML,尽量复用 providers 已有的 path 定义。"""
    cfg = providers.cc_switch_config_path()
    if not cfg.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print("未安装 pyyaml,无法读取 cc-switch 配置")
        return {}
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"读取 {cfg} 失败:{e}")
        return {}
    return data if isinstance(data, dict) else {}


def _extract_providers(data: dict) -> dict[str, dict]:
    """汇总 cc-switch 里的第三方供应商定义,同 providers._provider_entries(include_web=False)。"""
    out: dict[str, dict] = {}
    providers_dict = data.get("providers")
    if isinstance(providers_dict, dict):
        for name, entry in providers_dict.items():
            if isinstance(entry, dict):
                out[str(name)] = entry
    custom = data.get("custom_providers")
    if isinstance(custom, list):
        for entry in custom:
            if isinstance(entry, dict) and entry.get("name"):
                out[str(entry["name"])] = entry
    # 过滤官方名和明显无效条目;同时兼容老配置里的 camelCase(baseUrl/apiKey)
    filtered: dict[str, dict] = {}
    for name, entry in out.items():
        if name.lower() in providers._OFFICIAL_NAMES:
            continue
        base_url = (entry.get("base_url") or entry.get("baseUrl") or "").strip()
        model = (entry.get("model") or "").strip()
        api_key = (entry.get("api_key") or entry.get("apiKey") or "").strip()
        if not model or not base_url:
            print(f"跳过 {name}:缺少 base_url 或 model")
            continue
        filtered[name] = {"base_url": base_url, "api_key": api_key, "model": model, "label": name}
    return filtered


def migrate_cc_switch_to_web_providers(dry_run: bool = False) -> list[str]:
    """读取 cc-switch 配置,导入到 settings_store.web_providers。

    返回实际导入的供应商名列表。
    """
    data = _load_yaml()
    entries = _extract_providers(data)
    if not entries:
        print("没有需要迁移的 cc-switch 第三方供应商")
        return []

    imported: list[str] = []
    for name, cfg in entries.items():
        existing = {p["name"] for p in settings_store.list_web_providers()}
        if name in existing:
            print(f"跳过 {name}:设置页已存在同名供应商")
            continue
        if dry_run:
            print(f"[dry-run] 将导入 {name}: model={cfg['model']}, base_url={cfg['base_url']}")
            imported.append(name)
            continue
        err = settings_store.upsert_web_provider(name, cfg)
        if err:
            print(f"导入 {name} 失败:{err}")
            continue
        print(f"已导入 {name}: model={cfg['model']}")
        imported.append(name)
    return imported


if __name__ == "__main__":
    migrate_cc_switch_to_web_providers(dry_run="--dry-run" in sys.argv)
