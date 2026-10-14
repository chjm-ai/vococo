// 用现有全局 Playwright 验证独立原型，不启动或连接正式服务。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const output = path.resolve(__dirname, '../../../data/library-demo');
fs.mkdirSync(output, { recursive: true });
const source = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const bundle = source.replace('<link rel="stylesheet" href="./base.css">', () => `<style>${fs.readFileSync(path.join(__dirname, 'base.css'), 'utf8')}</style>`)
  .replace('<link rel="stylesheet" href="./demo.css">', () => `<style>${fs.readFileSync(path.join(__dirname, 'demo.css'), 'utf8')}</style>`)
  .replace('<script src="./demo.js" defer></script>', '')
  .replace('</body>', () => `<script>${fs.readFileSync(path.join(__dirname, 'demo.js'), 'utf8')}</script></body>`);
fs.writeFileSync(path.join(output, 'library-demo.html'), bundle);
const url = pathToFileURL(path.join(output, 'library-demo.html')).href;
let browser;
const errors = [];
const requests = [];
const count = page => page.locator('#works .work-card').count();
const click = (page, selector) => page.locator(selector).click();
const hasOverflow = page => page.evaluate(() => document.documentElement.scrollWidth > innerWidth);

async function checkInteractions(page) {
  await page.goto(url);
  assert.equal(await count(page), 8);
  await click(page, '[data-type="image"]');
  assert.equal(await count(page), 2);
  await page.locator('#search').fill('不存在的词');
  assert.equal(await count(page), 0);
  assert.equal(await page.locator('#empty-state').isVisible(), true);
  await click(page, '#empty-clear');
  await click(page, '[data-agent="AI 咨询"]');
  assert.equal(await count(page), 1);
  await click(page, '#clear-filters');
  await click(page, '[data-id="sample-film"][data-action="pin"]');
  assert.equal(await page.locator('[data-count-state="pinned"]').textContent(), '3');
  await click(page, '[data-id="sample-linx"][data-action="more"]');
  await click(page, '[data-id="sample-linx"][data-action="hide"]');
  assert.equal(await count(page), 7);
  await click(page, '#undo');
  assert.equal(await count(page), 8);
  await click(page, '[data-state="hidden"]');
  assert.equal(await count(page), 2);
  await click(page, '[data-id="sample-hidden-film"][data-action="more"]');
  await click(page, '[data-id="sample-hidden-film"][data-action="hide"]');
  assert.equal(await count(page), 1);
  await click(page, '#undo');
  assert.equal(await count(page), 2);
  await click(page, '#clear-filters');
  await page.locator('#sort').selectOption('oldest');
  assert.equal(await page.locator('.work-card').first().getAttribute('data-work-id'), 'sample-brand');
  await click(page, '#view-list');
  assert.equal(await page.locator('#works').getAttribute('data-layout'), 'list');
}

async function checkDetails(page) {
  await page.reload();
  await click(page, '[data-id="sample-linx"][data-action="open"]');
  assert.equal(await page.locator('#detail-title').textContent(), 'LinxAI 品牌首页');
  await click(page, '#source-toggle');
  assert.equal(await page.locator('#source-note').isVisible(), true);
  await page.locator('#detail-agent').selectOption('Vococo 开发');
  await click(page, '#detail-hide');
  assert.equal(await page.locator('#detail-hide').textContent(), '恢复作品');
  await click(page, '#undo');
  assert.equal(await page.locator('#detail-hide').textContent(), '隐藏作品');
  await click(page, '#reference-work');
  await page.locator('#composer-input').fill('继续做一个版本');
  await click(page, '#composer-send');
  assert.match(await page.locator('#composer-result').textContent(), /未发送至真实会话/);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#detail-dialog').isVisible(), false);
  assert.equal(await page.evaluate(() => document.activeElement.dataset.id), 'sample-linx');
}

async function checkLink(page) {
  await click(page, '#collect-link');
  await page.locator('#link-title').fill('<img src=x onerror=alert(1)>');
  await page.locator('#link-url').fill('javascript:alert(1)');
  await click(page, '#submit-link');
  assert.equal(await page.locator('#link-dialog').isVisible(), true);
  await page.locator('#link-url').fill('https://example.com/?a=1&b=2');
  await click(page, '#submit-link');
  assert.equal(await count(page), 9);
  assert.equal(await page.locator('.card-title img').count(), 0);
  await click(page, '[data-id="local-1"][data-action="open"]');
  assert.equal(await page.locator('#detail-title').textContent(), '<img src=x onerror=alert(1)>');
  assert.equal(await page.locator('#open-link').getAttribute('href'), 'https://example.com/?a=1&b=2');
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true }));
  await click(page, '#copy-link');
  assert.equal(await page.locator('#copy-fallback').isVisible(), true);
  await page.keyboard.press('Escape');
  await click(page, '#simulate-add');
  assert.equal(await count(page), 10);
  await page.reload();
  assert.equal(await count(page), 8);
}

async function checkViewport(page, width) {
  await page.setViewportSize({ width, height: 920 });
  await page.reload();
  assert.equal(await hasOverflow(page), false, `页面横向溢出 ${width}`);
  const tabHeights = await page.locator('.type-tabs button').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().top));
  assert.equal(new Set(tabHeights).size, 1, `类型标签换行 ${width}`);
  if (width <= 760) {
    await click(page, '#open-nav');
    assert.equal(await page.locator('#sidebar #status-filters, #sidebar #agent-filters').count(), 0);
    await page.keyboard.press('Escape');
    await click(page, '[data-agent="内容创作"]');
    assert.equal(await count(page), 2);
    await click(page, '#clear-filters');
  }
  await click(page, '[data-id="sample-linx"][data-action="open"]');
  assert.equal(await page.locator('#detail-dialog').evaluate(n => n.scrollWidth > n.clientWidth), false, `详情溢出 ${width}`);
  await page.keyboard.press('Escape');
  await click(page, '#view-list');
  assert.equal(await hasOverflow(page), false, `列表横向溢出 ${width}`);
  await click(page, '#view-grid');
  if (width === 390) await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  console.log(`PASS ${width}px: 网格/列表/类型栏/详情${width <= 760 ? '/移动筛选' : ''}`);
}

async function main() {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1060 }, colorScheme: 'light' });
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
  await checkInteractions(page);
  console.log('PASS 类型/Agent/搜索/空状态/置顶/隐藏/恢复/撤销/排序/列表');
  await checkDetails(page);
  console.log('PASS 详情/来源/归属修改/详情撤销/引用演示/焦点恢复');
  await checkLink(page);
  console.log('PASS URL协议/输入转义/收录链接/复制回退/模拟新增/刷新重置');
  for (const width of [320, 375, 390, 768, 1280]) await checkViewport(page, width);
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.reload();
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  await click(page, '[data-id="sample-linx"][data-action="open"]');
  await page.screenshot({ path: path.join(output, 'detail.png') });
  await page.keyboard.press('Escape');
  await click(page, '#theme-toggle');
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await page.screenshot({ path: path.join(output, 'dark.png'), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(requests, []);
  console.log('PASS 深色主题，无 JS 错误，无 HTTP 请求');
  console.log('单文件 Demo 与截图：' + output);
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => browser?.close());
