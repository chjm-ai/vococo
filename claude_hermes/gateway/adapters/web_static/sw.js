/* Wazir Service Worker —— 只为 Web Push 通知服务(不做离线缓存,页面始终实时读盘)。
   iOS 16.4+ 已装到主屏的 PWA 才会收到推送;后端用 VAPID 密钥把消息发到浏览器推送网关,
   浏览器唤醒本 worker 触发下面的 'push' 事件。 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

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
