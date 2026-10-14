'use strict';

// 明确的假数据，不读取历史会话、不请求接口、不做浏览器持久化。
const AGENTS = ['AI 设计', 'Vococo 开发', '内容创作', 'AI 咨询'];
const AGENT_CLASSES = ['agent-design', 'agent-dev', 'agent-content', 'agent-consult'];
const TYPES = { web: '网页', image: '图片', video: '视频', document: '文档' };
const TYPE_ICONS = { web: 'globe', image: 'image', video: 'film', document: 'file' };
const STATUS_NAMES = { all: '全部作品', pinned: '已置顶', hidden: '已隐藏' };
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  board: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>',
  library: '<path d="M4 4v16M9 4v16m5-15 5 14M3 20h18"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  pin: '<path d="m9 3 6 0-1 7 4 4H6l4-4-1-7ZM12 14v7"/>',
  eyeOff: '<path d="m3 3 18 18M10 5c5-1 9 5 11 7l-3 3M6 6 1 12s4 7 11 7l4-2M10 10a3 3 0 0 0 4 4"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  moon: '<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z"/>',
  link: '<path d="m10 13 4-4M8 15l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 3 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(1 1) scale(.9)"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  list: '<path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4m10 0h4M3 16h4m10 0h4"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 13h8M8 17h6"/>',
  external: '<path d="M14 3h7v7m0-7L10 14M10 5H4v15h15v-6"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  chat: '<path d="M21 15a3 3 0 0 1-3 3H9l-6 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v9Z"/>',
};
const SAMPLES = [
  { id: 'sample-linx', title: 'LinxAI 品牌首页', type: 'web', agent: AGENTS[0], art: 'web', tags: ['品牌', '网站'], date: '2026-10-12T10:00:00Z', filename: 'linxai-home-demo.html', description: '以自然绿和留白构成的品牌首页方向。封面为手工排版示意，并非真实网页截图。', pinned: true, hidden: false },
  { id: 'sample-poster', title: '纸间·夏日海报', type: 'image', agent: AGENTS[0], art: 'poster', tags: ['海报', '排版'], date: '2026-10-11T10:00:00Z', filename: 'summer-poster-demo.svg', description: '赤陶色、纸张弧线与中文大字的夏日海报实验。只有本页示意，没有真实图片原件。', pinned: true, hidden: false },
  { id: 'sample-film', title: '产品广告分镜', type: 'video', agent: AGENTS[2], art: 'film', tags: ['广告', '分镜'], date: '2026-10-10T10:00:00Z', filename: 'product-storyboard-demo.mp4', description: '用三组光影与构图探索产品广告节奏。这里只展示视频分镜示意，不包含可播放的视频。', pinned: false, hidden: false },
  { id: 'sample-plan', title: 'AI 咨询服务方案', type: 'document', agent: AGENTS[3], art: 'document', tags: ['方案', '咨询'], date: '2026-10-09T10:00:00Z', filename: 'ai-consulting-demo.pdf', description: '从问题梳理到落地路径的服务方案封面。内容为假数据，不代表任何真实客户项目。', pinned: false, hidden: false },
  { id: 'sample-nature', title: '自然观察手册', type: 'document', agent: AGENTS[2], art: 'nature', tags: ['植物', '手册'], date: '2026-10-08T10:00:00Z', filename: 'field-notes-demo.pdf', description: '一本关于日常植物与慢观察的编辑设计练习。植物图形为内联矢量示意。', pinned: false, hidden: false },
  { id: 'sample-fabric', title: '面料灵感展示页', type: 'web', agent: AGENTS[0], art: 'fabric', tags: ['面料', '网站'], date: '2026-10-07T10:00:00Z', filename: 'fabric-studio-demo.html', description: '暖沙、橄榄绿与纸白的面料灵感页面方向。色块代替真实面料，仅供界面评审。', pinned: false, hidden: false },
  { id: 'sample-components', title: '界面组件实验', type: 'web', agent: AGENTS[1], art: 'components', tags: ['界面', '组件'], date: '2026-10-06T10:00:00Z', filename: 'interface-lab-demo.html', description: '按钮、开关与卡片的小型组合实验。不是正式组件库，也未关联任何生产文件。', pinned: false, hidden: false },
  { id: 'sample-brand', title: '品牌视觉方向', type: 'image', agent: AGENTS[0], art: 'brand', tags: ['品牌', '视觉'], date: '2026-10-05T10:00:00Z', filename: 'brand-direction-demo.svg', description: '围绕字形、色彩与留白的一组视觉方向。仅呈现虚构品牌的排版示意。', pinned: false, hidden: false },
  { id: 'sample-hidden-film', title: '创作日常·片头草稿', type: 'video', agent: AGENTS[2], art: 'film', tags: ['草稿', '分镜'], date: '2026-10-04T10:00:00Z', filename: 'intro-draft-demo.mp4', description: '用于验证恢复操作的隐藏分镜草稿。不存在真实视频文件，恢复不会执行任何文件操作。', pinned: false, hidden: true },
  { id: 'sample-hidden-plan', title: '咨询访谈提纲·旧版', type: 'document', agent: AGENTS[3], art: 'document', tags: ['访谈', '旧版'], date: '2026-10-03T10:00:00Z', filename: 'interview-outline-demo.pdf', description: '用于验证隐藏视图的旧版提纲示例。隐藏只改变本页显示状态，不会删除原文件。', pinned: false, hidden: true },
];
let works = SAMPLES.map(item => ({ ...item, tags: [...item.tags] }));
const filters = { status: 'all', agent: 'all', type: 'all', query: '', sort: 'recent', view: 'grid' };
let selectedId = null;
let sequence = 0;
let undoAction = null;
const dialogOpeners = new WeakMap();
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const selectedWork = () => works.find(item => item.id === selectedId);

function esc(value) {
  return String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ICONS.file}</svg>`;
}

function agentClass(agent) {
  return AGENT_CLASSES[AGENTS.indexOf(agent)] || '';
}

function renderAgentControls() {
  const agents = ['all', ...AGENTS];
  $('#agent-filters').innerHTML = agents.map(agent => `<button class="filter-row ${agentClass(agent)}" type="button" data-agent="${esc(agent)}" aria-pressed="${agent === 'all'}"><span class="agent-mark" aria-hidden="true"></span><span>${esc(agent === 'all' ? '全部 Agent' : agent)}</span><span class="nav-count" data-count-agent="${esc(agent)}"></span></button>`).join('');
  for (const selector of ['#link-agent', '#detail-agent']) {
    $(selector).replaceChildren(...AGENTS.map(agent => new Option(agent, agent)));
  }
}

function matchesStatus(item, status = filters.status) {
  if (status === 'hidden') return item.hidden;
  if (status === 'pinned') return item.pinned && !item.hidden;
  return !item.hidden;
}

function baseResults() {
  const query = filters.query.trim().toLocaleLowerCase();
  return works.filter(item => matchesStatus(item)
    && (filters.agent === 'all' || item.agent === filters.agent)
    && (!query || [item.title, item.agent, ...item.tags].join(' ').toLocaleLowerCase().includes(query)));
}

function sortWorks(items) {
  return [...items].sort((left, right) => {
    if (filters.sort === 'name') return left.title.localeCompare(right.title, 'zh-CN');
    if (filters.sort === 'oldest') return left.date.localeCompare(right.date);
    return Number(right.pinned) - Number(left.pinned) || right.date.localeCompare(left.date);
  });
}

function renderCounts(base) {
  $$('[data-count-state]').forEach(node => {
    node.textContent = works.filter(item => matchesStatus(item, node.dataset.countState)).length;
  });
  $$('[data-count-agent]').forEach(node => {
    node.textContent = works.filter(item => matchesStatus(item) && (node.dataset.countAgent === 'all' || item.agent === node.dataset.countAgent)).length;
  });
  $$('[data-count-type]').forEach(node => {
    node.textContent = base.filter(item => node.dataset.countType === 'all' || item.type === node.dataset.countType).length;
  });
}

function renderSelection() {
  for (const [attribute, value] of Object.entries({ state: filters.status, agent: filters.agent, type: filters.type, view: filters.view })) {
    $$(`[data-${attribute}]`).forEach(button => button.setAttribute('aria-pressed', String(button.dataset[attribute] === value)));
  }
  $('#works').dataset.layout = filters.view;
  $('#results-heading').textContent = STATUS_NAMES[filters.status];
  $('#sort').value = filters.sort;
}

function focusedCardControl() {
  const active = document.activeElement;
  if (!active?.closest('#works')) return null;
  return { id: active.dataset.id, action: active.dataset.action };
}

function restoreCardFocus(previous) {
  if (!previous) return;
  const control = $$('[data-action][data-id]').find(node => node.dataset.id === previous.id && node.dataset.action === previous.action);
  (control || $('#results-heading')).focus({ preventScroll: true });
}

function render() {
  const previous = focusedCardControl();
  const base = baseResults();
  const shown = sortWorks(base.filter(item => filters.type === 'all' || item.type === filters.type));
  renderCounts(base);
  renderSelection();
  $('#works').innerHTML = shown.map(cardMarkup).join('');
  $('#empty-state').hidden = shown.length > 0;
  $('#result-count').textContent = `${shown.length} 件 · ${filters.agent === 'all' ? '全部 Agent' : filters.agent}`;
  restoreCardFocus(previous);
}

function cardMarkup(item) {
  const title = esc(item.title);
  const pinLabel = item.pinned ? '取消置顶' : '置顶';
  const hideLabel = item.hidden ? '恢复作品' : '隐藏作品';
  return `<article class="work-card" data-work-id="${item.id}">
    <button class="card-open" type="button" data-action="open" data-id="${item.id}" aria-label="查看作品：${title}" aria-haspopup="dialog">
      <div class="cover" aria-hidden="true">${artMarkup(item)}</div>
      <div class="card-copy"><h3 class="card-title">${title}</h3>
        <div class="card-meta ${agentClass(item.agent)}"><span class="agent-mark" aria-hidden="true"></span><span>${esc(item.agent)}</span><span>· ${esc(item.tags[0])}</span></div>
        <div class="card-bottom"><span class="type-label">${icon(TYPE_ICONS[item.type])}${TYPES[item.type]}</span><span>${item.hidden ? '已隐藏' : '示例'}</span><time datetime="${item.date}">${item.date.slice(0, 10).replaceAll('-', '.')}</time></div>
      </div>
    </button>
    <div class="card-actions">
      <button class="icon-button" type="button" data-action="pin" data-id="${item.id}" aria-label="${pinLabel}：${title}" aria-pressed="${item.pinned}" title="${pinLabel}">${icon('pin')}</button>
      <button class="icon-button" type="button" data-action="more" data-id="${item.id}" aria-label="更多操作：${title}" aria-expanded="false" aria-controls="menu-${item.id}">${icon('more')}</button>
      <div id="menu-${item.id}" class="more-menu" role="group" aria-label="作品操作" hidden><button type="button" data-action="hide" data-id="${item.id}">${hideLabel}</button></div>
    </div>
  </article>`;
}

function artMarkup(item) {
  return `<div class="art art-${item.art}">${artContent(item)}</div>`;
}

function artContent(item) {
  const content = {
    web: '<div class="art-top">linx<span class="art-kicker">/ AI STUDIO</span></div><div class="art-orbit"></div><div class="art-heading">Built for<span>the next.</span></div><div class="art-foot"><span class="art-pill">探索可能 ↗</span><span>网页示意</span></div>',
    poster: '<div class="art-top art-kicker">PAPER / SUMMER</div><div class="art-heading">纸间<br>夏日</div><div class="art-foot"><span>让日光，停在纸上。</span><span>排版示意</span></div>',
    film: '<div class="art-top art-kicker">STORY / 03</div><div class="film-frames"><div class="film-frame"><span>01</span></div><div class="film-frame"><span>02</span></div><div class="film-frame"><span>03</span></div></div><div class="art-foot"><span>光、形与片刻</span><span>分镜示意</span></div>',
    document: '<div class="art-top art-kicker">IDEAS INTO ACTION</div><div class="paper-sheet"><span>服务提案 / 示例</span><div class="art-heading">从问题<br>到新的可能</div><div class="paper-lines"></div></div>',
    nature: `<div class="art-top art-kicker">FIELD NOTES / 01</div><div class="art-heading">自然<br>观察手册</div>${botanicalMarkup()}<div class="art-foot"><span>慢一点，看到更多。</span><span>示意</span></div>`,
    fabric: '<div class="art-top art-kicker">TACTILE / STUDIO</div><div class="fabric-swatches"><i></i><i></i><i></i></div><div class="art-heading">触摸<br>生活的肌理</div><div class="art-foot"><span>Materials & feelings</span><span>示意</span></div>',
    components: '<div class="art-top art-kicker">INTERFACE / LAB</div><div class="art-heading">Small things.<br>Better days.</div><div class="component-sample"><i></i><span>Design in detail</span><b></b></div><div class="art-foot"><span>组件实验</span><span>示意</span></div>',
    brand: '<div class="art-top art-kicker">A QUIETER IDENTITY</div><div class="art-heading">form &<br>feeling.</div><div class="art-foot"><span class="palette"><i></i><i></i><i></i></span><span>视觉示意</span></div>',
  };
  if (item.art === 'link') return `<div class="art-top art-kicker">LINK / LOCAL DEMO</div><div class="art-heading">${esc(item.title)}</div><div class="art-foot"><span>外部链接 · 未抓取网页</span></div>`;
  return content[item.art] || content.brand;
}

function botanicalMarkup() {
  return '<svg class="botanical" viewBox="0 0 160 240" fill="currentColor" aria-hidden="true"><path d="M76 233C99 166 73 103 110 15" fill="none" stroke="currentColor" stroke-width="2"/><path d="M93 72C58 70 48 42 55 21c34 9 44 28 38 51ZM93 77c8-39 35-44 56-43-2 31-20 44-56 43ZM85 124C48 122 26 92 32 69c34 4 59 24 53 55ZM84 137c12-39 42-47 65-40-9 35-29 44-65 40ZM86 179c-42 0-63-26-63-53 35 0 61 20 63 53ZM83 195c10-30 38-43 64-38-8 30-31 40-64 38Z"/></svg>';
}

function resetFilters() {
  Object.assign(filters, { status: 'all', agent: 'all', type: 'all', query: '', sort: 'recent' });
  $('#search').value = '';
  render();
}

function closeMenus(restore = false) {
  const open = $('[data-action="more"][aria-expanded="true"]');
  $$('.more-menu').forEach(menu => { menu.hidden = true; });
  $$('[data-action="more"]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  if (restore && open) open.focus();
}

function toggleMenu(button) {
  const wasOpen = button.getAttribute('aria-expanded') === 'true';
  closeMenus();
  if (wasOpen) return;
  button.setAttribute('aria-expanded', 'true');
  const menu = document.getElementById(button.getAttribute('aria-controls'));
  menu.hidden = false;
  menu.querySelector('button').focus();
}

function togglePin(item) {
  item.pinned = !item.pinned;
  render();
  refreshDetailActions();
  showToast(`${item.pinned ? '已置顶' : '已取消置顶'}「${item.title}」· 仅本页示例`);
}

function toggleHidden(item) {
  // 每次隐藏/恢复都保存操作前的值，覆盖旧撤销，避免连续操作撤销错对象。
  undoAction = { id: item.id, hidden: item.hidden };
  item.hidden = !item.hidden;
  render();
  refreshDetailActions();
  showToast(`已${item.hidden ? '隐藏' : '恢复'}「${item.title}」，未删除原文件。`, true);
}

function undoHidden() {
  if (!undoAction) return;
  const item = works.find(work => work.id === undoAction.id);
  if (!item) return;
  item.hidden = undoAction.hidden;
  undoAction = null;
  render();
  refreshDetailActions();
  showToast(`已撤销，${item.title}恢复为${item.hidden ? '隐藏' : '可见'}状态。`);
  $('#dismiss-toast').focus();
}

function toastHost() {
  return $$('dialog[open]').at(-1) || document.body;
}

function showToast(message, canUndo = false) {
  if (!canUndo) undoAction = null;
  toastHost().append($('#toast'));
  $('#toast-message').textContent = message;
  $('#undo').hidden = !canUndo;
  $('#toast').hidden = false;
}

function openDialog(dialog) {
  closeMenus();
  dialogOpeners.set(dialog, document.activeElement);
  dialog.showModal();
  if (!$('#toast').hidden) dialog.append($('#toast'));
}

function onDialogClose(dialog) {
  if (dialog.id === 'nav-dialog') $('#sidebar-slot').append($('#sidebar'));
  if ($('#toast').parentElement === dialog) toastHost().append($('#toast'));
  let opener = dialogOpeners.get(dialog);
  if (opener?.dataset.id && !opener.isConnected) {
    const previous = opener.dataset;
    opener = $$('[data-action][data-id]').find(node => node.dataset.id === previous.id && node.dataset.action === previous.action);
  }
  const fallback = dialog.id === 'nav-dialog' ? $('#library-nav') : $('#results-heading');
  if (opener?.isConnected && opener.getClientRects().length) opener.focus({ preventScroll: true });
  else fallback.focus({ preventScroll: true });
}

function renderDetail(item) {
  $('#detail-cover').innerHTML = `<div class="cover" aria-hidden="true">${artMarkup(item)}</div><p class="cover-caption">${item.type === 'video' ? '视频分镜示意，不可播放。' : '排版示意封面，不是真实历史作品。'}</p>`;
  $('#detail-title').textContent = item.title;
  $('#detail-description').textContent = item.description;
  $('#detail-kind').textContent = `${TYPES[item.type]} / 示例作品`;
  $('#detail-agent').value = item.agent;
  $('#detail-date').textContent = item.date.slice(0, 10);
  $('#detail-filename').textContent = `${item.filename}（示例名称，无真实附件）`;
  $('#detail-tags').replaceChildren(...item.tags.map(tag => {
    const span = document.createElement('span');
    span.className = 'tag';
    span.textContent = tag;
    return span;
  }));
  $('#source-note').textContent = `示例会话：「${item.agent} · ${item.title}创作讨论」。此文本仅用于说明来源位置，没有真实会话路由。`;
  renderDetailLink(item);
  refreshDetailActions();
}

function refreshDetailActions() {
  const item = selectedWork();
  if (!item) return;
  $('#detail-pin').textContent = item.pinned ? '取消置顶' : '置顶作品';
  $('#detail-pin').setAttribute('aria-pressed', String(item.pinned));
  $('#detail-hide').textContent = item.hidden ? '恢复作品' : '隐藏作品';
}

function renderDetailLink(item) {
  $('#detail-link-section').hidden = !item.url;
  $('#copy-fallback').hidden = true;
  $('#open-link').removeAttribute('href');
  $('#detail-url').textContent = item.url || '';
  $('#copy-text').value = item.url || '';
  if (item.url) $('#open-link').href = item.url;
}

function openDetail(id) {
  selectedId = id;
  const item = selectedWork();
  if (!item) return;
  renderDetail(item);
  $('#source-note').hidden = true;
  $('#source-toggle').setAttribute('aria-expanded', 'false');
  $('#demo-composer').hidden = true;
  $('#reference-work').setAttribute('aria-expanded', 'false');
  $('#demo-composer').reset();
  $('#composer-result').textContent = '';
  openDialog($('#detail-dialog'));
  $('#detail-dialog').scrollTop = 0;
}

function toggleSource() {
  const note = $('#source-note');
  note.hidden = !note.hidden;
  $('#source-toggle').setAttribute('aria-expanded', String(!note.hidden));
}

function openComposer() {
  const item = selectedWork();
  $('#demo-composer').hidden = false;
  $('#reference-work').setAttribute('aria-expanded', 'true');
  $('#attachment-chip').textContent = `示例附件 · ${item.title} / ${item.filename}`;
  $('#composer-input').focus();
}

function sendComposer(event) {
  event.preventDefault();
  const input = $('#composer-input');
  input.setCustomValidity(input.value.trim() ? '' : '请填写演示消息。');
  if (!input.reportValidity()) return;
  $('#composer-result').textContent = '演示完成，未发送至真实会话';
}

function changeAgent() {
  const item = selectedWork();
  item.agent = $('#detail-agent').value;
  render();
  $('#source-note').textContent = `示例会话：「${item.agent} · ${item.title}创作讨论」。仅用于演示，没有真实路由；更换归属不迁移任何真实会话。`;
  showToast(`归属已改为 ${item.agent}，仅影响本页示例。`);
}

function createLocalWork(values) {
  sequence += 1;
  return { id: `local-${sequence}`, title: `新的灵感练习 ${sequence}`, type: 'image', agent: AGENTS[0], art: 'brand', tags: ['模拟收录'], date: new Date().toISOString(), filename: `local-example-${sequence}.svg`, description: '点击「模拟新作品」生成的本地假数据。没有真实同步或自动收录，刷新后消失。', pinned: false, hidden: false, ...values };
}

function revealNewWork(item) {
  works.unshift(item);
  resetFilters();
  // 既保留置顶优先，也让新作品在长列表中立即进入视野。
  const card = $$('[data-action="open"]').find(button => button.dataset.id === item.id);
  card?.focus({ preventScroll: true });
  card?.scrollIntoView({ block: 'nearest' });
}

function simulateNew() {
  const item = createLocalWork({});
  revealNewWork(item);
  showToast(`已模拟收录「${item.title}」· 仅本页，刷新后重置。`);
}

function openLinkForm() {
  $('#link-form').reset();
  $('#link-title').setCustomValidity('');
  $('#link-url').setCustomValidity('');
  $('#link-error').textContent = '';
  openDialog($('#link-dialog'));
}

function validateLink() {
  const title = $('#link-title');
  const input = $('#link-url');
  title.setCustomValidity(title.value.trim() ? '' : '请填写作品标题，不能只有空格。');
  input.setCustomValidity('');
  let url = null;
  try {
    url = new URL(input.value.trim());
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('protocol');
    if (!url.hostname) throw new Error('host');
  } catch {
    input.setCustomValidity('仅支持有效的 http:// 或 https:// 网址。');
  }
  if (!$('#link-form').reportValidity()) {
    $('#link-error').textContent = title.validationMessage || input.validationMessage;
    return null;
  }
  return { title: title.value.trim(), url: url.href };
}

function submitLink(event) {
  event.preventDefault();
  const values = validateLink();
  if (!values) return;
  const item = createLocalWork({ ...values, type: 'web', art: 'link', agent: $('#link-agent').value, tags: ['手动链接', '本地示例'], filename: 'collected-link-demo.url', description: '用户手动加入本页的外部链接。未抓取网页或封面，未保存到真实作品库，刷新后消失。' });
  $('#link-dialog').close();
  revealNewWork(item);
  // 关闭事件触发后仍回到新增作品，避免焦点被收录入口重新夺走。
  dialogOpeners.set($('#link-dialog'), $$('[data-action="open"]').find(button => button.dataset.id === item.id));
  showToast(`已加入「${item.title}」· 仅本页示例，未抓取网页。`);
}

async function copyLink() {
  const item = selectedWork();
  if (!item?.url) return;
  const id = item.id;
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(item.url);
    if (selectedId === id && $('#detail-dialog').open) showToast('链接已复制。');
  } catch {
    if (selectedId !== id || !$('#detail-dialog').open) return;
    $('#copy-fallback').hidden = false;
    $('#copy-text').value = item.url;
    selectLinkText();
  }
}

function selectLinkText() {
  $('#copy-text').focus();
  $('#copy-text').select();
  $('#copy-text').setSelectionRange(0, $('#copy-text').value.length);
}

function syncThemeButton() {
  const theme = document.documentElement.dataset.theme;
  const isDark = theme ? theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  $('#theme-toggle').innerHTML = icon(isDark ? 'sun' : 'moon');
  $('#theme-toggle').setAttribute('aria-pressed', String(isDark));
  $('#theme-toggle').setAttribute('aria-label', `切换为${isDark ? '浅色' : '深色'}主题`);
  $('#theme-toggle').title = `${theme ? '手动' : '跟随系统'} · 当前${isDark ? '深色' : '浅色'}`;
}

function toggleTheme() {
  document.documentElement.dataset.theme = $('#theme-toggle').getAttribute('aria-pressed') === 'true' ? 'light' : 'dark';
  syncThemeButton();
}

function openNavigation() {
  const dialog = $('#nav-dialog');
  dialog.append($('#sidebar'));
  openDialog(dialog);
  $('#close-nav').focus();
}

function bindFilters() {
  $('#status-filters').addEventListener('click', event => {
    const button = event.target.closest('[data-state]');
    if (button) { filters.status = button.dataset.state; render(); }
  });
  $('#agent-filters').addEventListener('click', event => {
    const button = event.target.closest('[data-agent]');
    if (button) { filters.agent = button.dataset.agent; render(); }
  });
  $('#type-tabs').addEventListener('click', event => {
    const button = event.target.closest('[data-type]');
    if (button) { filters.type = button.dataset.type; render(); }
  });
  $('#search').addEventListener('input', event => { filters.query = event.target.value; render(); });
  $('#sort').addEventListener('change', event => { filters.sort = event.target.value; render(); });
  $$('[data-view]').forEach(button => button.addEventListener('click', () => { filters.view = button.dataset.view; render(); }));
  ['#clear-filters', '#empty-clear', '#library-nav'].forEach(selector => $(selector).addEventListener('click', resetFilters));
}

function bindCardActions() {
  $('#works').addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const item = works.find(work => work.id === button.dataset.id);
    if (!item) return;
    const actions = { open: () => openDetail(item.id), pin: () => togglePin(item), hide: () => toggleHidden(item), more: () => toggleMenu(button) };
    actions[button.dataset.action]?.();
  });
  document.addEventListener('click', event => { if (!event.target.closest('.card-actions')) closeMenus(); });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !$('[data-action="more"][aria-expanded="true"]')) return;
    event.preventDefault();
    closeMenus(true);
  });
  document.addEventListener('focusin', event => {
    const open = $('[data-action="more"][aria-expanded="true"]');
    if (open && !open.parentElement.contains(event.target)) closeMenus();
  });
}

function bindDialogs() {
  $$('dialog').forEach(dialog => {
    dialog.addEventListener('close', () => onDialogClose(dialog));
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const outside = event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) dialog.close();
    });
  });
  const closing = { '#close-detail': '#detail-dialog', '#close-link': '#link-dialog', '#cancel-link': '#link-dialog', '#close-nav': '#nav-dialog' };
  Object.entries(closing).forEach(([button, dialog]) => $(button).addEventListener('click', () => $(dialog).close()));
  $('#open-nav').addEventListener('click', openNavigation);
  matchMedia('(min-width: 761px)').addEventListener('change', event => {
    if (event.matches && $('#nav-dialog').open) $('#nav-dialog').close();
  });
}

function bindDetailActions() {
  $('#detail-pin').addEventListener('click', () => togglePin(selectedWork()));
  $('#detail-hide').addEventListener('click', () => toggleHidden(selectedWork()));
  $('#detail-agent').addEventListener('change', changeAgent);
  $('#source-toggle').addEventListener('click', toggleSource);
  $('#reference-work').addEventListener('click', openComposer);
  $('#demo-composer').addEventListener('submit', sendComposer);
  $('#composer-input').addEventListener('input', event => event.target.setCustomValidity(''));
  $('#copy-link').addEventListener('click', copyLink);
  $('#select-link').addEventListener('click', selectLinkText);
}

function bindLinkForm() {
  $('#collect-link').addEventListener('click', openLinkForm);
  $('#link-form').addEventListener('submit', submitLink);
  ['#link-title', '#link-url'].forEach(selector => $(selector).addEventListener('input', event => {
    event.target.setCustomValidity('');
    $('#link-error').textContent = '';
  }));
  $('#link-form').addEventListener('invalid', event => {
    $('#link-error').textContent = event.target.id === 'link-url' ? '请输入完整的 HTTP(S) 网址，不支持 javascript: 或 data:。' : event.target.validationMessage;
  }, true);
}

function init() {
  $$('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); });
  renderAgentControls();
  bindFilters();
  bindCardActions();
  bindDialogs();
  bindDetailActions();
  bindLinkForm();
  $('#simulate-add').addEventListener('click', simulateNew);
  $('#theme-toggle').addEventListener('click', toggleTheme);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncThemeButton);
  $('#undo').addEventListener('click', undoHidden);
  $('#dismiss-toast').addEventListener('click', () => {
    $('#toast').hidden = true;
    undoAction = null;
    const closeButton = $('dialog[open] button[aria-label^="关闭"]');
    (closeButton || $('#results-heading')).focus({ preventScroll: true });
  });
  syncThemeButton();
  render();
}

init();
