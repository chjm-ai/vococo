// 浏览器不直接联网；所有资源先检查固定 origin/路径，再用固定 IP 代取。
'use strict';
const http = require('node:http');
const https = require('node:https');
const dns = require('node:dns').promises;
const net = require('node:net');
// Playwright 在 POSIX 默认 detached 启动 Chrome，必须关闭才能由父进程组硬清理。
const childProcess = require('node:child_process');
const originalSpawn = childProcess.spawn;
childProcess.spawn = (command, args, options) => originalSpawn(command, args, {...options, detached: false});
const {chromium} = require('playwright');
const denied = new net.BlockList();
for (const [address, prefix] of [
  ['0.0.0.0',8], ['10.0.0.0',8], ['100.64.0.0',10], ['127.0.0.0',8],
  ['169.254.0.0',16], ['172.16.0.0',12], ['192.0.0.0',24], ['192.0.2.0',24],
  ['192.168.0.0',16], ['198.18.0.0',15], ['198.51.100.0',24],
  ['203.0.113.0',24], ['224.0.0.0',3], ['192.88.99.0',24],
]) denied.addSubnet(address, prefix);

function allowed(raw, base) {
  try {
    const url = new URL(raw);
    if (url.origin !== base.origin || url.username || url.password) return false;
    // 不接受编码路径、反斜线或非当前站点路径，避免服务端二次解码/穿越。
    if (url.pathname.includes('%') || raw.includes('\\') || !url.pathname.startsWith(base.pathname)) return false;
    return ['http:', 'https:'].includes(url.protocol);
  } catch { return false; }
}

async function addressFor(base) {
  if (base.hostname === '127.0.0.1') return '127.0.0.1';
  const addresses = await dns.resolve4(base.hostname);
  if (!addresses.length || addresses.some(ip => !net.isIPv4(ip) || denied.check(ip)))
    throw new Error('DNS 指向非公网地址');
  return addresses[0];
}

function fetchResource(url, address) {
  return new Promise((resolve, reject) => {
    const transport = url.protocol === 'https:' ? https : http;
    const request = transport.get(url, {
      agent: false, lookup: (_host, _options, done) => done(null, address, 4),
      headers: {'Accept': '*/*'}, timeout: 5000,
    }, response => {
      const chunks = []; let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 10 * 1024 * 1024) response.destroy(new Error('资源过大'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => resolve({status: response.statusCode,
        headers: {'content-type': response.headers['content-type'] || 'application/octet-stream'},
        body: Buffer.concat(chunks)}));
    });
    request.on('timeout', () => request.destroy(new Error('资源超时')));
    request.on('error', reject);
  });
}

async function capture(raw, output, publicOrigin) {
  const base = new URL(raw);
  const publicTarget = typeof publicOrigin === 'string' && /^https:\/\/[A-Za-z0-9.-]+$/.test(publicOrigin) &&
    base.origin === publicOrigin && /^\/[a-z0-9]+(?:-[a-z0-9]+)*\/$/.test(base.pathname) &&
    raw === publicOrigin + base.pathname;
  if (!publicTarget &&
      !/^http:\/\/127\.0\.0\.1:\d+\/pub\/[a-z0-9]+(?:-[a-z0-9]+)*\/$/.test(raw))
    throw new Error('非法截图目标');
  const address = await addressFor(base);
  const context = await chromium.launchPersistentContext(require('node:path').join(require('node:path').dirname(output), 'profile'), {
    channel: 'chrome', headless: true, serviceWorkers: 'block', acceptDownloads: false,
    viewport: {width: 1440, height: 900},
    proxy: {server: 'http://127.0.0.1:1'},
    args: ['--proxy-bypass-list=<-loopback>', '--disable-background-networking',
      '--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']});
  let blocked = 0; let requests = 0; let total = 0;
  try {
    if (typeof context.routeWebSocket !== 'function') throw new Error('Playwright 版本缺少 WebSocket 隔离能力');
    await context.routeWebSocket(/.*/, socket => { blocked++; socket.close(); });
    await context.route('**/*', async route => {
      const request = route.request();
      if (request.method() !== 'GET' || !allowed(request.url(), base) || ++requests > 100) {
        blocked++; return route.abort();
      }
      try {
        const resource = await fetchResource(new URL(request.url()), address);
        // 不跟随重定向，包括同源；避免路由、DNS和 URL 校验旁路。
        if (resource.status >= 300 && resource.status < 400) throw new Error('拒绝重定向');
        total += resource.body.length;
        if (total > 30 * 1024 * 1024 || resource.status >= 400) throw new Error('资源不可用');
        await route.fulfill(resource);
      } catch { blocked++; await route.abort(); }
    });
    const page = await context.newPage();
    await page.goto(raw, {waitUntil: 'domcontentloaded', timeout: 15000});
    await page.waitForTimeout(800);
    await page.screenshot({path: output, type: 'png', timeout: 5000});
    return {blocked_requests: blocked, warning: blocked ? '外链、重定向或非受控资源已阻断，封面可能不完整' : ''};
  } finally { await context.close(); }
}

module.exports = {allowed, addressFor, fetchResource, capture};
if (require.main === module) capture(process.argv[2], process.argv[3], process.argv[4])
  .then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => { console.error(error.message); process.exitCode = 1; });
