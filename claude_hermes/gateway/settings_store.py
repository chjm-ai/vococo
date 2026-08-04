"""Web 端运行时设置：技能白名单 / MCP 开关(内置 + 外部) / 模型&供应商 的持久化与生效计算。

落一个 JSON(data/web_settings.json)当"运行时覆盖层"。agent 每轮构建 options
时读它来决定挂哪些 skill、挂哪些 MCP —— 所以在网页改完设置【下一轮就生效，
不用重启进程】。文件不存在 = 全走 config 默认(和没这套东西时行为一致)。

设计要点:
- skills 有 default / custom 两态。default 完全跟随 config.SKILLS(通常是 None=全量),
  不改今天的 token 行为;用户在设置页第一次动某个 skill 才"固化"成显式白名单(custom),
  之后就按白名单挂。这样"不碰就零副作用,碰了才显式接管"。
- 隐藏(hidden)只影响设置列表折叠,与是否加载无关。
- MCP:内置 hermes 一个总开关;外部 server 存 stdio/sse/http 配置 + enabled 位,
  开启的那些直接并进 ClaudeAgentOptions.mcp_servers(SDK 0.2.110 原生支持外部 server)。
- 模型/供应商:web_extra_models 是官方新模型档位还没进代码前的手动补录(id+label,
  按订阅走,不需要 key,id 是键、重复 id 直接覆盖 label=编辑);web_providers 是第三方
  端点(base_url+api_key+model),直接落 data/web_settings.json。
  避免两边互相覆写。disabled_builtin_models 是代码里 MODEL_CHOICES 硬编码档位的隐藏
  名单(不能真删常量,只能摘出选择器,可随时恢复)。providers.py 每次都现读现并,
  同样【改完下一轮就生效】。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .. import config

_PATH: Path = config.DATA_DIR / "web_settings.json"
_SKILLS_DIR: Path = Path.home() / ".claude" / "skills"
_LOCK = threading.Lock()  # 网页多请求可能并发读改写,加锁保证 JSON 不被写花

_DEFAULTS: dict = {
    "skills_mode": "default",   # default=跟随 config.SKILLS;custom=用下面的白名单
    "skills_enabled": [],       # custom 态生效的显式白名单
    "skills_hidden": [],        # 仅设置列表折叠用
    "hermes_mcp_enabled": True,
    "external_mcp": {},         # name -> {type,command,args,env,url,headers,enabled}
    "web_default_model": "",    # web 端上次选定的模型;新会话没显式选就用它(空=回落 config.MODEL)
    "web_effort": "",           # web 端思考深度(high/max,空=不传,交给供应商默认);见 get_web_effort
    "web_extra_models": [],     # 设置页手动加的官方模型档位:[{id,label}](新模型发布,代码没跟上时用)
    "web_providers": {},        # 设置页手动加的第三方服务商:name -> {base_url,api_key,model,label}
    "disabled_builtin_models": [],  # 代码里硬编码的官方档位(MODEL_CHOICES),用户在设置页
                                     # 手动隐藏掉的那些 id——不能真删代码常量,只能从选择器里摘掉
}


# ── 读写 ────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        raw = {}
    data = {**_DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    # 保证子结构类型正确(手改坏了也不崩)——同时必须复制出新对象,不能直接拿 _DEFAULTS
    # 里的 list/dict 引用:文件不存在时 data[key] 会等于 _DEFAULTS[key] 本身,调用方一
    # mutate(如 list.append/dict[k]=v)就把模块级默认值永久污染了。
    for key in ("skills_enabled", "skills_hidden", "web_extra_models", "disabled_builtin_models"):
        data[key] = list(data[key]) if isinstance(data.get(key), list) else []
    for key in ("external_mcp", "web_providers"):
        data[key] = dict(data[key]) if isinstance(data.get(key), dict) else {}
    return data


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_PATH)  # 原子替换,避免读到写一半的半截 JSON


# ── 技能扫描 ────────────────────────────────────────────────────────────
def _parse_front_matter(text: str) -> dict:
    """从 SKILL.md 头部 --- YAML --- 里抠出 name / description(不引入 yaml 依赖)。

    兼容 YAML 块标量:很多 skill 写成 `description: >-` 然后把正文放到后续缩进行,
    此时得把缩进行拼起来,否则只会拿到 ">-" 这种指示符。
    """
    out: dict = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    body = lines[1:]
    i, n = 0, len(body)
    while i < n:
        ln = body[i]
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][\w-]*):(.*)$", ln)
        if not m:
            i += 1
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val in (">", ">-", ">+", "|", "|-", "|+"):
            # 块标量:收集后续缩进行,直到下一个顶格 key 或 ---
            parts: list[str] = []
            i += 1
            while i < n:
                nx = body[i]
                if nx.strip() == "---" or (nx.strip() and not nx[0].isspace()):
                    break
                if nx.strip():
                    parts.append(nx.strip())
                i += 1
            val = " ".join(parts)
        else:
            val = val.strip('"').strip("'")
            i += 1
        if key in ("name", "description"):
            out[key] = val
    return out


def _scan_skills() -> list[dict]:
    """扫 ~/.claude/skills/*/SKILL.md，返回 [{name, description}]（跟随符号链接）。"""
    found: list[dict] = []
    try:
        entries = sorted(_SKILLS_DIR.iterdir(), key=lambda p: p.name.lower())
    except (FileNotFoundError, OSError):
        return found
    for d in entries:
        if d.name.startswith("."):
            continue
        skill_md = d / "SKILL.md"  # is_file() 会自动跟随符号链接
        try:
            if not skill_md.is_file():
                continue
            text = skill_md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _parse_front_matter(text)
        name = fm.get("name") or d.name
        found.append({"name": name, "description": fm.get("description", "")})
    # 同名去重(符号链接可能重复),保留首个
    seen: set[str] = set()
    uniq: list[dict] = []
    for s in found:
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        uniq.append(s)
    return uniq


def _available_names() -> set[str]:
    return {s["name"] for s in _scan_skills()}


def _base_enabled_set(available: set[str]) -> set[str]:
    """config.SKILLS 语义下"当前启用集":None/'all'=全部可用;list=白名单。"""
    base = config.SKILLS
    if base is None or base == "all":
        return set(available)
    if isinstance(base, list):
        return set(base)
    return set(available)


# ── 技能:对外查询 / 修改 ────────────────────────────────────────────────
def list_skills() -> list[dict]:
    """给设置页用:每个可用 skill 的 name/description/enabled/hidden。"""
    d = _load()
    skills = _scan_skills()
    available = {s["name"] for s in skills}
    hidden = set(d["skills_hidden"])
    if d["skills_mode"] == "custom":
        enabled = set(d["skills_enabled"])
    else:
        enabled = _base_enabled_set(available)
    out = []
    for s in skills:
        out.append({
            "name": s["name"],
            "description": s["description"],
            "enabled": s["name"] in enabled,
            "hidden": s["name"] in hidden,
        })
    return out


def skills_mode() -> str:
    return _load()["skills_mode"]


def set_skill(name: str, enabled: bool | None = None, hidden: bool | None = None) -> None:
    with _LOCK:
        d = _load()
        if hidden is not None:
            hs = set(d["skills_hidden"])
            hs.add(name) if hidden else hs.discard(name)
            d["skills_hidden"] = sorted(hs)
        if enabled is not None:
            if d["skills_mode"] != "custom":
                # 第一次动 → 把当前(default 语义下的)启用集固化成显式白名单
                d["skills_enabled"] = sorted(_base_enabled_set(_available_names()))
                d["skills_mode"] = "custom"
            es = set(d["skills_enabled"])
            es.add(name) if enabled else es.discard(name)
            d["skills_enabled"] = sorted(es)
        _save(d)


def reset_skills() -> None:
    """技能回到"跟随默认(通常全量)",清掉显式白名单;隐藏保留。"""
    with _LOCK:
        d = _load()
        d["skills_mode"] = "default"
        d["skills_enabled"] = []
        _save(d)


# ── web 端默认模型 ──────────────────────────────────────────────────────
def get_web_default_model() -> str:
    """web 端上次选定的模型;没设过返回空串(调用方回落 config.MODEL)。"""
    return _load().get("web_default_model") or ""


def set_web_default_model(model: str) -> None:
    """web 端切模型时记住它:新开的 web 会话没显式选就默认用这个。"""
    with _LOCK:
        d = _load()
        d["web_default_model"] = model or ""
        _save(d)


# ── web 端思考深度(effort)─────────────────────────────────────────────
def get_web_effort() -> str:
    """web 端上次选定的思考深度(high/max);没设过返回空串(调用方回落:不传,交供应商默认)。"""
    e = _load().get("web_effort") or ""
    return e if e in ("high", "max") else ""


def set_web_effort(effort: str) -> None:
    """web 端切思考深度时记住它;非法值(非 high/max)视为清空。"""
    with _LOCK:
        d = _load()
        d["web_effort"] = effort if effort in ("high", "max") else ""
        _save(d)


# ── 设置页手动加的官方模型档位 ────────────────────────────────────────────
def list_web_extra_models() -> list[dict]:
    """给设置页/available_models 用:[{id,label}]。"""
    return list(_load()["web_extra_models"])


def upsert_web_extra_model(model_id: str, label: str) -> str | None:
    """新增/编辑一个官方模型档位(id 是键,重复 id 直接覆盖 label);返回错误说明(None=成功)。"""
    model_id = (model_id or "").strip()
    if not model_id:
        return "缺少模型 id"
    label = (label or "").strip() or model_id
    with _LOCK:
        d = _load()
        models = d["web_extra_models"]
        for m in models:
            if m.get("id") == model_id:
                m["label"] = label
                break
        else:
            models.append({"id": model_id, "label": label})
        d["web_extra_models"] = models
        _save(d)
    return None


def remove_web_extra_model(model_id: str) -> None:
    with _LOCK:
        d = _load()
        d["web_extra_models"] = [m for m in d["web_extra_models"] if m.get("id") != model_id]
        _save(d)


# ── 设置页手动隐藏的内置模型档位(MODEL_CHOICES 里代码写死的那些) ──────────
def list_disabled_builtin_models() -> list[str]:
    return list(_load()["disabled_builtin_models"])


def set_builtin_model_disabled(model_id: str, disabled: bool) -> None:
    with _LOCK:
        d = _load()
        s = set(d["disabled_builtin_models"])
        s.add(model_id) if disabled else s.discard(model_id)
        d["disabled_builtin_models"] = sorted(s)
        _save(d)


# ── 设置页手动加的第三方服务商 ────────────────────────────────────────────
def list_web_providers() -> list[dict]:
    """给设置页用:每个供应商的 name + 配置(含 api_key 明文)。"""
    d = _load()
    out = [{"name": name, **cfg} for name, cfg in d["web_providers"].items()]
    out.sort(key=lambda x: x["name"].lower())
    return out


def clean_web_provider(body: dict) -> tuple[dict, str | None]:
    """校验设置页提交的供应商字段;返回 (cfg, 错误)——错误非空时 cfg 为 {}。"""
    base_url = (body.get("base_url") or "").strip()
    model = (body.get("model") or "").strip()
    if not base_url:
        return {}, "缺少 base_url"
    if not model:
        return {}, "缺少 model"
    from urllib.parse import urlsplit

    scheme = urlsplit(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        return {}, "base_url 必须是 http(s) 协议"
    api_key = (body.get("api_key") or "").strip()
    label = (body.get("label") or "").strip()
    # vision=是否支持直传图片(Codex OAuth 代理背后的 GPT 系勾选);只接受 "1",
    # 其余一律落空串,避免脏值进存储
    vision = "1" if str(body.get("vision", "")).strip() == "1" else ""
    # mgmt_key=本地 Codex 代理的 Management API 钥匙(填了才有 GPT 订阅额度查询,
    # 见 providers.codex_usage);空串=不查额度
    mgmt_key = (body.get("mgmt_key") or "").strip()
    return {"base_url": base_url, "api_key": api_key, "model": model, "label": label,
            "vision": vision, "mgmt_key": mgmt_key}, None


def upsert_web_provider(name: str, body: dict) -> str | None:
    """新增/覆盖一个设置页供应商;返回错误说明(None=成功)。"""
    name = (name or "").strip()
    if not name:
        return "缺少服务商名称"
    cfg, err = clean_web_provider(body)
    if err:
        return err
    with _LOCK:
        d = _load()
        d["web_providers"][name] = cfg
        _save(d)
    return None


def remove_web_provider(name: str) -> None:
    with _LOCK:
        d = _load()
        d["web_providers"].pop(name, None)
        _save(d)


def web_providers_raw() -> dict[str, dict]:
    """给 providers.py 用:原始 name→entry 字典(字段名和旧 cc-switch 条目同形状,便于迁移)。"""
    return dict(_load()["web_providers"])


def effective_skills() -> list[str] | str | None:
    """传给 ClaudeAgentOptions.skills 的值。default 态原样返回 config.SKILLS。"""
    d = _load()
    if d["skills_mode"] == "custom":
        return list(d["skills_enabled"])
    return config.SKILLS


# ── MCP:对外查询 / 修改 ─────────────────────────────────────────────────
def hermes_enabled() -> bool:
    return bool(_load()["hermes_mcp_enabled"])


def set_hermes(enabled: bool) -> None:
    with _LOCK:
        d = _load()
        d["hermes_mcp_enabled"] = bool(enabled)
        _save(d)


def list_external() -> list[dict]:
    """外部 MCP 列表(给设置页;含 enabled 位)。"""
    d = _load()
    out = []
    for name, cfg in d["external_mcp"].items():
        out.append({"name": name, **cfg})
    out.sort(key=lambda x: x["name"].lower())
    return out


def clean_external_config(body: dict) -> tuple[dict, str | None]:
    """把外部(如 Web 设置页)提交的字段清洗成合法的 stdio/sse/http MCP 配置;
    返回 (cfg, 错误)——错误非空时 cfg 为 {}。"""
    typ = (body.get("type") or "stdio").strip().lower()
    enabled = bool(body.get("enabled", True))
    if typ == "stdio":
        # 远程注册 stdio MCP = 让服务端拉起任意子进程,等同远程 RCE(审计 web#6 / 2-5)。
        # 默认拒绝,除非显式 WEB_ALLOW_STDIO_MCP=1。sse/http 型不受此限。
        if not config.WEB_ALLOW_STDIO_MCP:
            return {}, ("出于安全,已禁止从 Web 注册本地 stdio MCP(可执行任意命令)。"
                        "如确需,请在 .env 设 WEB_ALLOW_STDIO_MCP=1 后重启;"
                        "或改用 sse/http 型 MCP。")
        command = (body.get("command") or "").strip()
        if not command:
            return {}, "stdio 类型需要 command"
        raw_args = body.get("args")
        if isinstance(raw_args, str):
            args = raw_args.split()
        elif isinstance(raw_args, list):
            args = [str(a) for a in raw_args]
        else:
            args = []
        env = body.get("env") if isinstance(body.get("env"), dict) else {}
        env = {str(k): str(v) for k, v in env.items()}
        return (
            {"type": "stdio", "command": command, "args": args,
             "env": env, "enabled": enabled},
            None,
        )
    if typ in ("sse", "http"):
        url = (body.get("url") or "").strip()
        if not url:
            return {}, f"{typ} 类型需要 url"
        headers = body.get("headers") if isinstance(body.get("headers"), dict) else {}
        headers = {str(k): str(v) for k, v in headers.items()}
        return (
            {"type": typ, "url": url, "headers": headers, "enabled": enabled},
            None,
        )
    return {}, f"不支持的类型:{typ}"


def upsert_external(name: str, body: dict) -> str | None:
    """新增/覆盖一个外部 MCP:先清洗校验 body,合法才落库。返回 None=成功,
    否则返回错误说明(调用方原样透传给用户,半成品配置不会落库)。

    校验以前只在 web.py 的 handler 里做(先调 _clean_mcp_config 再传清洗结果进来);
    直接调这个函数会绕过校验。2026-07-23 把校验收口进本函数,保证这条不变量
    不管谁调用都成立——本模块自己的数据自己保证合法,而不是指望调用方记得先清洗。
    """
    cfg, err = clean_external_config(body)
    if err:
        return err
    with _LOCK:
        d = _load()
        d["external_mcp"][name] = cfg
        _save(d)
    return None


def remove_external(name: str) -> None:
    with _LOCK:
        d = _load()
        d["external_mcp"].pop(name, None)
        _save(d)


def set_external_enabled(name: str, enabled: bool) -> None:
    with _LOCK:
        d = _load()
        if name in d["external_mcp"]:
            d["external_mcp"][name]["enabled"] = bool(enabled)
            _save(d)


def effective_external_mcp() -> dict:
    """开启的外部 server → {name: sdk_config}（剥掉内部用的 enabled 字段）。"""
    d = _load()
    servers: dict = {}
    for name, cfg in d["external_mcp"].items():
        if not cfg.get("enabled", True):
            continue
        c = {k: v for k, v in cfg.items() if k != "enabled" and v not in (None, "", [], {})}
        c.setdefault("type", "stdio")
        servers[name] = c
    return servers
