"""平台级元数据库(platform.db,仅 server 模式):租户 / 用户 / 登录态。

与 per-tenant state.db 的分工(见 docs/design/server-edition-tech-plan.md §4.1):
- 平台数据(账号、租户、P1 的钱包流水)要跨租户全表扫(对账/统计)→ 单库行级 tenant_id。
- 客户会话数据 → per-tenant 文件物理隔离(见 memory/_db.py)。
personal 模式不使用本模块(doctor/本地流程不碰);server 模式由 Web 鉴权中间件
与 `vococo tenant` CLI 使用。

密码用 pbkdf2-hmac-sha256(stdlib,60 万轮):不引第三方依赖(服务器镜像要瘦身),
强度对 v1 邀请制够用;将来自助注册放开再评估换 argon2。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time

from .. import config

_DB: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants(
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',   -- active/suspended(欠费/违规停用)
  wallet_cny_balance REAL NOT NULL DEFAULT 0,  -- P1 计费启用,先建列
  monthly_quota_cny REAL,                  -- P2 月配额;NULL=不限
  markup REAL NOT NULL DEFAULT 5.0,        -- 计费加成倍数(售价=成本×markup)
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
  user_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner',      -- owner/member(v1 只有 owner)
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions(
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cron_jobs(
  job_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  spec TEXT NOT NULL  -- 完整 job dict 的 JSON(与 personal 的 cron_jobs.json 同构)
);
"""

_PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SEC = 30 * 24 * 3600  # 登录态 30 天

# 租户 id 会进文件路径(data/tenants/<tid>/)和会话 key 前缀(t:<tid>:),
# 必须是不含冒号/路径分隔符的 slug。
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        path = config.DATA_DIR / "platform.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(path), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.executescript(_SCHEMA)
    return _DB


def reset() -> None:
    """测试专用:关连接清缓存(配合 monkeypatch DATA_DIR 的临时库)。"""
    global _DB
    if _DB is not None:
        _DB.close()
        _DB = None


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ── 密码 ─────────────────────────────────────────────────────────────────
def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), digest)


# ── 租户 / 用户 CRUD(v1 邀请制:只有 CLI/后台创建,无自助注册)───────────────
def create_tenant(tenant_id: str, name: str) -> dict:
    tenant_id = (tenant_id or "").strip().lower()
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"租户 id「{tenant_id}」非法:小写字母/数字/短横线,2-32 位,字母或数字开头")
    if not (name or "").strip():
        raise ValueError("租户名不能为空")
    c = _conn()
    if c.execute("SELECT 1 FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone():
        raise ValueError(f"租户「{tenant_id}」已存在")
    c.execute(
        "INSERT INTO tenants(tenant_id,name,created_at) VALUES (?,?,?)",
        (tenant_id, name.strip(), int(time.time())),
    )
    c.commit()
    return get_tenant(tenant_id)  # type: ignore[return-value]


def get_tenant(tenant_id: str) -> dict | None:
    return _row(
        _conn()
        .execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,))
        .fetchone()
    )


def list_tenants() -> list[dict]:
    rows = _conn().execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def create_user(tenant_id: str, email: str, password: str, role: str = "owner") -> dict:
    if get_tenant(tenant_id) is None:
        raise ValueError(f"租户「{tenant_id}」不存在")
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("email 非法")
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    c = _conn()
    if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise ValueError(f"邮箱「{email}」已注册")
    user_id = secrets.token_hex(8)
    c.execute(
        "INSERT INTO users(user_id,tenant_id,email,password_hash,role,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, tenant_id, email, _hash_password(password), role, int(time.time())),
    )
    c.commit()
    return _row(
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    )  # type: ignore[return-value]


def authenticate(email: str, password: str) -> dict | None:
    """邮箱+密码校验;成功返回 user 行(租户被停用/用户被停用也视为失败)。"""
    row = _conn().execute(
        "SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)
    ).fetchone()
    if row is None or row["status"] != "active":
        return None
    if not _verify_password(password or "", row["password_hash"]):
        return None
    tenant = get_tenant(row["tenant_id"])
    if tenant is None or tenant["status"] != "active":
        return None
    return dict(row)


# ── 登录态 ───────────────────────────────────────────────────────────────
def create_session(user_id: str) -> str:
    token = secrets.token_hex(32)
    _conn().execute(
        "INSERT INTO web_sessions(token,user_id,expires_at) VALUES (?,?,?)",
        (token, user_id, int(time.time()) + SESSION_TTL_SEC),
    )
    _conn().commit()
    return token


def resolve_session(token: str) -> tuple[dict, dict] | None:
    """cookie token → (user, tenant);过期/不存在/停用一律 None。顺手清过期行。"""
    if not token:
        return None
    c = _conn()
    now = int(time.time())
    c.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
    c.commit()
    row = c.execute(
        "SELECT user_id FROM web_sessions WHERE token=? AND expires_at>=?",
        (token, now),
    ).fetchone()
    if row is None:
        return None
    user = _row(c.execute("SELECT * FROM users WHERE user_id=?", (row["user_id"],)).fetchone())
    if user is None or user["status"] != "active":
        return None
    tenant = get_tenant(user["tenant_id"])
    if tenant is None or tenant["status"] != "active":
        return None
    return user, tenant


def delete_session(token: str) -> None:
    c = _conn()
    c.execute("DELETE FROM web_sessions WHERE token=?", (token,))
    c.commit()


# ── cron 任务(server 模式替代全局 cron_jobs.json;personal 不用这组)────────────
def cron_jobs_all() -> list[dict]:
    """全部租户的 cron 任务(调度器 tick 用);每个 job dict 附带 "_tenant" 键标明归属。"""
    import json

    rows = _conn().execute("SELECT tenant_id, spec FROM cron_jobs").fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            job = json.loads(r["spec"])
        except (json.JSONDecodeError, ValueError):
            continue
        job["_tenant"] = r["tenant_id"]
        out.append(job)
    return out


def cron_jobs_for(tenant_id: str) -> list[dict]:
    import json

    rows = _conn().execute(
        "SELECT spec FROM cron_jobs WHERE tenant_id=?", (tenant_id,)
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["spec"]))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def cron_jobs_save(tenant_id: str, jobs: list[dict]) -> None:
    """整组替换某租户的任务(事务内 DELETE+INSERT,与 personal 的整文件覆写同语义)。"""
    import json

    c = _conn()
    with c:  # 连接上下文管理器 = 事务:异常自动 ROLLBACK,正常结束自动 COMMIT
        c.execute("DELETE FROM cron_jobs WHERE tenant_id=?", (tenant_id,))
        for job in jobs:
            job = {k: v for k, v in job.items() if k != "_tenant"}  # 落库不带瞬态键
            c.execute(
                "INSERT INTO cron_jobs(job_id, tenant_id, spec) VALUES (?,?,?)",
                (job["id"], tenant_id, json.dumps(job, ensure_ascii=False)),
            )
