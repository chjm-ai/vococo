"""Web Push 推送 —— 把通知发到「已装到主屏」的手机 PWA(iOS 16.4+ / Android / 桌面)。

和 SSE 的区别:SSE 只在页面开着时能推;Web Push 走浏览器推送网关(APNs / FCM),
页面关了、锁屏了也能弹系统通知。这正是「发完指令切走、跑完想被叫回来」的场景。

流程:
1. 前端 serviceWorker.pushManager.subscribe(applicationServerKey=VAPID 公钥) 拿到订阅串;
2. POST /push/subscribe 存到 data/push_subs.json;
3. 后端用 VAPID 私钥对每个订阅调 webpush() 发出;网关把负载送到设备唤醒 SW。

依赖 pywebpush;未安装或未配 VAPID 密钥则整体降级为「不推送」,不影响其余功能。
生成密钥:  python -m vococo.gateway.adapters.web_push --gen-keys
把打印出的两串填进项目根 .env 的 VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path


def _cfg():
    """延迟加载 config:让 --gen-keys 能在不触发订阅认证的情况下单独跑。"""
    from ... import config

    return config


try:  # pywebpush 是可选依赖:没装就整体降级
    from pywebpush import WebPushException, webpush

    _HAS_PYWEBPUSH = True
except Exception:  # pragma: no cover - 环境未装
    webpush = None  # type: ignore
    WebPushException = Exception  # type: ignore
    _HAS_PYWEBPUSH = False


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keys() -> tuple[str, str]:
    """生成一对 VAPID 密钥(P-256),返回 (公钥, 私钥),都是 URL-safe base64 无填充。

    公钥 = 65 字节未压缩点(给前端 applicationServerKey);私钥 = 32 字节标量。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    pub_point = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    priv_val = priv.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(pub_point), _b64url(priv_val)


class PushManager:
    """订阅存储 + 群发 + 死订阅自动清理。进程内单例(见文件末 PUSH)。"""

    def __init__(self) -> None:
        # 不在 __init__ 里碰 config/磁盘:让 --gen-keys 能在不加载配置时导入本模块
        self._path: Path | None = None
        self._lock = threading.Lock()
        self._subs: list[dict] | None = None

    def _ensure(self) -> None:
        if self._subs is None:
            self._path = _cfg().DATA_DIR / "push_subs.json"
            self._subs = self._load()

    # ── 持久化 ───────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [s for s in data if isinstance(s, dict) and s.get("endpoint")]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._subs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ── 配置状态 ─────────────────────────────────────────────────────────
    @staticmethod
    def is_configured() -> bool:
        """装了 pywebpush 且配齐了 VAPID 公私钥,才算能推。"""
        c = _cfg()
        return bool(_HAS_PYWEBPUSH and c.VAPID_PUBLIC_KEY and c.VAPID_PRIVATE_KEY)

    def public_config(self) -> dict:
        """给前端 /push/config:是否可用 + 公钥 + 当前设备是否已订阅由前端自查。"""
        self._ensure()
        return {
            "enabled": self.is_configured(),
            "vapidPublicKey": _cfg().VAPID_PUBLIC_KEY if self.is_configured() else "",
            "count": len(self._subs or []),
        }

    # ── 订阅增删 ─────────────────────────────────────────────────────────
    def add(self, sub: dict) -> bool:
        """存一个订阅。优先按 deviceId 去重覆盖,没带 deviceId 才退回按 endpoint。

        iOS 每次订阅失效后重新 subscribe() 会拿到全新 endpoint,单靠 endpoint 去重
        会导致同一台设备在列表里越攒越多(旧的永远躺尸,只有真送达时收到 404/410
        才会被清)。前端固定用 localStorage 存的 deviceId 标识"这是同一台设备",
        新订阅进来就顶替旧的,从源头掐掉重复。
        """
        ep = sub.get("endpoint")
        if not ep or not isinstance(sub.get("keys"), dict):
            return False
        device_id = sub.get("deviceId") or None
        ua = str(sub.get("ua") or "")[:200]
        self._ensure()
        with self._lock:
            def _stale(s: dict) -> bool:
                if device_id and s.get("deviceId") == device_id:
                    return True
                return s.get("endpoint") == ep

            self._subs = [s for s in self._subs if not _stale(s)]
            self._subs.append(
                {
                    "endpoint": ep,
                    "keys": sub["keys"],
                    "deviceId": device_id,
                    "ua": ua,
                    "subscribedAt": time.time(),
                }
            )
            self._save()
        return True

    def remove(self, endpoint: str) -> None:
        if not endpoint:
            return
        self._ensure()
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            if len(self._subs) != before:
                self._save()

    def list_public(self) -> list[dict]:
        """给设置页「已订阅设备」列表用:只吐能识别设备的元信息,不带订阅密钥。"""
        self._ensure()
        return [
            {
                "endpoint": s.get("endpoint", ""),
                "ua": s.get("ua", ""),
                "subscribedAt": s.get("subscribedAt"),
            }
            for s in (self._subs or [])
        ]

    def _prune(self, dead: set[str]) -> None:
        if not dead:
            return
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") not in dead]
            self._save()

    # ── 发送 ─────────────────────────────────────────────────────────────
    def _send_one(self, sub: dict, payload: str) -> str | None:
        """阻塞发送单个订阅。返回该 endpoint 若已失效(需清理),否则 None。"""
        c = _cfg()
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=c.VAPID_PRIVATE_KEY,
                # 每次新建 claims:pywebpush 会往里塞 exp,复用同一 dict 会串味
                vapid_claims={"sub": c.VAPID_SUBJECT},
                ttl=600,
                # 不设超时的话,一台设备的推送网关卡住会拖住整个群发(含 /push/test 这种
                # 同步等结果的请求),客户端最终看到"Load failed"——限时让它快速失败,别的设备照常收到。
                timeout=10,
            )
            return None
        except WebPushException as e:  # type: ignore
            status = getattr(getattr(e, "response", None), "status_code", None)
            # 404/410 = 订阅已注销(用户删了 PWA / 换设备),清掉
            if status in (404, 410):
                return sub.get("endpoint")
            return None
        except Exception:
            return None

    async def notify(
        self,
        title: str,
        body: str,
        *,
        conv: str = "main",
        kind: str = "",
        tag: str | None = None,
    ) -> int:
        """异步群发一条通知给所有订阅设备。返回成功送出的设备数。

        kind: done | approval | proactive | error —— 前端 SW 据此决定前台是否也弹。
        tag:  同一 tag 的通知在系统里互相替换(默认按会话+场景),避免刷屏。
        """
        self._ensure()
        if not self.is_configured() or not self._subs:
            return 0
        payload = json.dumps(
            {
                "title": title,
                "body": body[:180],
                "conv": conv,
                "kind": kind,
                "tag": tag or f"vococo-{conv}-{kind or 'msg'}",
                "url": f"/?conv={conv}",
            },
            ensure_ascii=False,
        )
        loop = asyncio.get_event_loop()
        subs = list(self._subs)
        results = await asyncio.gather(
            *(loop.run_in_executor(None, self._send_one, s, payload) for s in subs)
        )
        dead = {ep for ep in results if ep}
        self._prune(dead)
        return len(subs) - len(dead)


# 进程内单例:web.py / 调度器都从这里拿
PUSH = PushManager()


def _main() -> None:
    import sys

    if "--gen-keys" in sys.argv:
        pub, priv = generate_vapid_keys()
        print("# 把下面两行填进项目根目录的 .env(VAPID_SUBJECT 可选,建议填你的邮箱):")
        print(f"VAPID_PUBLIC_KEY={pub}")
        print(f"VAPID_PRIVATE_KEY={priv}")
        print("VAPID_SUBJECT=mailto:you@example.com")
    else:
        print("用法: python -m vococo.gateway.adapters.web_push --gen-keys")


if __name__ == "__main__":
    _main()
