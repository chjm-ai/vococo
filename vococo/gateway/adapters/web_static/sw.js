/* vococo Service Worker —— 两件事:
   1. Web Push 通知(iOS 16.4+ 装到主屏的 PWA 才收得到,后端 VAPID 推送唤醒本 worker);
   2. 应用外壳缓存(2026-07-23 提速):跨境隧道首字节要 1.5~3.5s、带宽 ~50KB/s,
      把 / 和静态资源缓存在本机,打开秒出界面,后台静默拉最新版;发现 / 变了就
      postMessage 通知页面弹"点击刷新"。/api /events /send 等动态接口一律不拦,照常走网络。
      2026-08-17:/ 从"缓存优先"改成"网络优先、超时退回缓存"——见下面 fetch 里的注释,
      此前部署新版本后要连刷两次页面才真正生效,一次排查"重启后无感重连"的修复时
      正被这层坑了一道(前端代码没变,是缓存了没刷新出来,误以为修复本身没生效)。 */

const SHELL_CACHE = "vococo-shell-v8";
const SHELL_NETWORK_TIMEOUT_MS = 1200;  // 超过此预算网络还没回应就退回缓存,不让跨境隧道用户干等
// 只缓存这份白名单里的路径(按 pathname 匹配,?v= 版本参数算在完整 URL 里)
// app-core/markdown/sidebar/settings/workbench/stream/composer/voice:从 index.html 拆出的功能块
const SHELL_PATHS = new Set([
  "/", "/styles.css", "/mascot.css", "/mascot.js", "/tool-card.js", "/manifest.json", "/favicon.ico",
  "/app-core.js", "/markdown.js", "/sidebar.js", "/settings.js", "/stats.js", "/workbench.js",
  "/stream.js", "/composer.js", "/voice.js",
  "/vococo-mark.svg", "/icon-192.png", "/icon-512.png",
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

  // 拉网络 + 更新缓存;命中 / 且 ETag 变了就 postMessage 通知所有窗口弹刷新提示
  // (两端 ETag 都由源站按内容哈希生成,直接比较即可判断内容是否真的变了)。
  // 返回值:网络异常(断网/超时)给 undefined,让调用方退回缓存;服务端给出的响应
  // (哪怕 404/500)原样透传——网络优先的本意就是信服务端当下的真实回答。
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
  // 走哪条 respondWith 分支都不影响这个后台任务必须完整跑完(更新缓存、发通知)
  event.waitUntil(revalidate.then(() => {}, () => {}));

  const cacheFallback = () =>
    caches
      .open(SHELL_CACHE)
      .then((c) => c.match(req))
      .then((cached) => cached || revalidate.then((r) => r || new Response("offline", { status: 503 })));

  if (url.pathname === "/") {
    // 网络优先、超时(SHELL_NETWORK_TIMEOUT_MS)退回缓存:保证【刷新一次】就拿到最新版本。
    // 此前 / 也走缓存优先,会先吃到缓存里的旧 HTML(里面 <script> 引用的还是旧 ?v=哈希),
    // 连带把旧 JS 也一起钉住——要连刷两次才真正生效。超时预算内网络没回应就照原样退回
    // 缓存,不让跨境隧道用户干等;网络请求仍在 waitUntil 里跑完更新缓存供下次刷新用。
    event.respondWith(
      Promise.race([
        revalidate,
        new Promise((resolve) => setTimeout(resolve, SHELL_NETWORK_TIMEOUT_MS)),
      ]).then((resp) => resp || cacheFallback())
    );
    return;
  }
  // 其余路径(?v=内容哈希 的版本化资源、manifest/favicon/icons 等):维持缓存优先——
  // 只要 / 能一次刷新就拿到最新的 ?v= 引用,这些路径天然没有"刷新也拿不到新版"的问题,
  // 缓存优先换来的秒开体验没理由放弃。
  event.respondWith(cacheFallback());
});

// 后端推来的负载:{title, body, tag, conv, url, kind}
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { d = {}; }
  const title = d.title || "vococo";
  const kind = d.kind || "";
  const options = {
    body: d.body || "",
    tag: d.tag || "vococo",          // 同 tag 会替换旧通知,避免同一会话刷屏
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
