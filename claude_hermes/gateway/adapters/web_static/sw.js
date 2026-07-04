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

  // 前台正在看:若已有窗口聚焦,就不弹系统通知(避免打扰),交给页面内提示
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      const focused = cs.some((c) => c.focused || c.visibilityState === "visible");
      // 审批/出错这类高优先级,即使前台也弹一下,确保不漏
      if (focused && kind !== "approval" && kind !== "error") {
        // 顺带通知页面刷新一下(可选),不弹 OS 通知
        cs.forEach((c) => c.postMessage({ type: "push", data: options.data, foreground: true }));
        return;
      }
      return self.registration.showNotification(title, options);
    })
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
