/* Wazir Service Worker —— 两件事:
   1. Web Push 通知(iOS 16.4+ 装到主屏的 PWA 才收得到,后端 VAPID 推送唤醒本 worker);
   2. 应用外壳缓存(2026-07-23 提速):跨境隧道首字节要 1.5~3.5s、带宽 ~50KB/s,
      把 / 和静态资源缓存在本机,打开秒出界面(stale-while-revalidate),后台
      静默拉最新版;发现 / 变了就 postMessage 通知页面弹"点击刷新"。
      /api /events /send 等动态接口一律不拦,照常走网络。 */

const SHELL_CACHE = "wazir-shell-v1";
// 只缓存这份白名单里的路径(按 pathname 匹配,?v= 版本参数算在完整 URL 里)
const SHELL_PATHS = new Set([
  "/", "/styles.css", "/tool-card.js", "/manifest.json", "/favicon.ico",
  "/wazir-mark.svg", "/icon-192.png", "/icon-512.png",
  "/icon-maskable-512.png", "/apple-touch-icon.png",
]);

self.addEventListener("install", (e) => {
  self.skipWaiting();
  // 预缓存外壳入口;失败无所谓(下次 fetch 会补),不能因此装不上 SW
  e.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.add("/")).catch(() => {})
  );
});
self.addEventListener("activate", (e) =>
  e.waitUntil(
    Promise.all([
      self.clients.claim(),
      // 清掉旧版本缓存桶(改 SHELL_CACHE 名字即整体作废)
      caches.keys().then((ks) =>
        Promise.all(ks.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))
      ),
    ])
  )
);

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !SHELL_PATHS.has(url.pathname)) return;

  // stale-while-revalidate:有缓存立刻回缓存,同时后台拉网络更新缓存;
  // 没缓存(首次)就等网络。网络层仍走浏览器 HTTP 缓存(ETag 304 白捡)。
  const revalidate = (async () => {
    let resp;
    try { resp = await fetch(req); } catch (_) { return undefined; }
    if (!resp || !resp.ok) return resp;
    const cache = await caches.open(SHELL_CACHE);
    const cached = await cache.match(req);
    // 同一路径的旧版本(不同 ?v=)清掉,别越攒越多
    const keys = await cache.keys();
    await Promise.all(
      keys
        .filter((k) => new URL(k.url).pathname === url.pathname && k.url !== req.url)
        .map((k) => cache.delete(k))
    );
    await cache.put(req, resp.clone());
    // 首页内容变了 → 通知所有窗口弹刷新提示(ETag 比对,双方都由源站按内容哈希生成)
    if (url.pathname === "/" && cached) {
      const oldTag = cached.headers.get("ETag");
      const newTag = resp.headers.get("ETag");
      if (oldTag && newTag && oldTag !== newTag) {
        const cs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        cs.forEach((c) => c.postMessage({ type: "shell-updated" }));
      }
    }
    return resp;
  })();
  event.waitUntil(revalidate.then(() => {}, () => {}));
  event.respondWith(
    caches
      .open(SHELL_CACHE)
      .then((c) => c.match(req))
      .then(
        (cached) =>
          cached ||
          revalidate.then(
            (r) => r || new Response("offline", { status: 503 })
          )
      )
  );
});

// 后端推来的负载:{title, body, tag, conv, url, kind}
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { d = {}; }
  const title = d.title || "Wazir";
  const kind = d.kind || "";
  const options = {
    body: d.body || "",
    tag: d.tag || "hermes",          // 同 tag 会替换旧通知,避免同一会话刷屏
    renotify: kind === "approval",   // 审批类即使替换也再次提醒
    data: { conv: d.conv || "main", url: d.url || "/", kind },
    icon: "/icon-192.png",
    badge: "/icon-192.png",
  };

  // iOS 有条不成文规则:SW 收到 push 却不调 showNotification() 攒够几次,系统会
  // 直接把订阅作废(https://dev.to/progressier/how-to-fix-ios-push-subscriptions-being-terminated-after-3-notifications-39a7)。
  // 之前"前台就不弹"的静默分支正好天天踩这条坑——每次你开着页面收到 done 通知都被吞掉,
  // 攒够几次订阅就没了。所以不管前台/后台都必须真弹;前台"别吵"的诉求靠 tag 覆盖 +
  // renotify:false 做静默替换(同 tag 不出声/不震动),而不是干脆不弹。
  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, options),
      self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
        const focused = cs.some((c) => c.focused || c.visibilityState === "visible");
        if (focused) cs.forEach((c) => c.postMessage({ type: "push", data: options.data, foreground: true }));
      }),
    ])
  );
});

// 点通知:聚焦已有窗口(并让它切到对应会话),没有就新开
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = data.url || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      for (const c of cs) {
        if ("focus" in c) {
          c.postMessage({ type: "open", conv: data.conv || "main" });
          return c.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
