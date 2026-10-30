"""静态预览的限时能力链接；不经过发布目录或作品库。"""
from __future__ import annotations

import base64
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import secrets
import sqlite3
import time

from aiohttp import web

from .. import config
from .web_auth import check_web_auth

TTL = 7 * 24 * 3600
MAX_BYTES = 200_000
MAX_SHARES = 100
POLICY = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow, noarchive", "X-Content-Type-Options": "nosniff"}


class StaticHTML(HTMLParser):
    # SVG 只开放静态图形和排版属性；不开放动画、foreignObject 或任意链接。
    tags = set("html head body title style div span p br hr h1 h2 h3 h4 h5 h6 section article header footer main aside nav ul ol li dl dt dd table thead tbody tfoot tr td th caption colgroup col pre code blockquote strong em b i u s small sub sup figure figcaption img a details summary".split())
    attrs = set("class id style title lang dir colspan rowspan scope width height alt open".split())

    svg_tags = set('svg g defs rect circle ellipse line polyline polygon path text tspan image linearGradient radialGradient stop clipPath mask marker use'.split())
    svg_attrs = set('x y x1 x2 y1 y2 cx cy r rx ry d dx dy points viewBox preserveAspectRatio transform fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-dasharray stroke-linecap stroke-linejoin opacity font-family font-size font-weight text-anchor dominant-baseline clip-path clipPathUnits mask marker-start marker-mid marker-end markerWidth markerHeight refX refY orient offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod role aria-label'.split())
    svg_names = {name.lower(): name for name in svg_tags | svg_attrs}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.in_style = False

    def safe_attribute(self, tag, key, value):
        image_url = (tag == 'img' and key == 'src') or (tag == 'image' and key in {'href', 'xlink:href'})
        if image_url:
            return bool(re.fullmatch(r'data:image/(?:png|jpeg|webp|gif);base64,[a-z0-9+/]+={0,2}', value or '', re.I))
        if re.search(r'\\|data\s*:', value or '', re.I):
            return False
        return key in self.attrs or (tag in self.svg_tags and key in self.svg_attrs)

    def handle_starttag(self, tag, attrs):
        tag = self.svg_names.get(tag, tag)
        if tag not in self.tags | self.svg_tags:
            return
        safe = [(self.svg_names.get(key, key), value) for key, value in attrs
                if self.safe_attribute(tag, self.svg_names.get(key, key), value)]
        # xlink:href 归一到 SVG2 href，不依赖输入是否声明 xlink 命名空间。
        safe = [('href' if key == 'xlink:href' else key, value) for key, value in safe]
        self.parts.append('<' + tag + ''.join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in safe) + '>')
        self.in_style = tag == 'style'

    def handle_endtag(self, tag):
        tag = self.svg_names.get(tag, tag)
        if tag in self.tags | self.svg_tags:
            self.parts.append(f'</{tag}>')
        self.in_style = False

    def handle_data(self, data):
        # 不解析任意 CSS 转义；含 data URL 或转义的样式整段丢弃，避免绕过位图白名单。
        if self.in_style and re.search(r'\\|data\s*:', data, re.I):
            return
        self.parts.append(data.replace('<', r'\3c ') if self.in_style else html.escape(data))


def valid_source(source):
    if not isinstance(source, str) or not source.strip():
        return False
    try:
        return len(source.encode()) <= MAX_BYTES
    except UnicodeError:
        return False


def sanitize(source):
    parser = StaticHTML()
    parser.feed(source)
    return '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + POLICY + '">' + ''.join(parser.parts)


class ShareStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, content TEXT NOT NULL, expires REAL NOT NULL)')
        path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        with self.connect() as db:
            db.execute('DELETE FROM shares WHERE expires <= ?', (time.time(),))

    def create(self, source):
        token, share_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        expires = time.time() + TTL
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM shares WHERE expires <= ?', (time.time(),))
            if db.execute('SELECT count(*) FROM shares').fetchone()[0] >= MAX_SHARES:
                raise ValueError('分享数量已达上限，请先撤销旧分享')
            db.execute('INSERT INTO shares VALUES (?, ?, ?, ?)', (share_id, hashlib.sha256(token.encode()).hexdigest(), sanitize(source), expires))
        return {"id": share_id, "path": "/preview-share#" + token, "expires": expires}

    def read(self, token, *, check=False):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        with self.connect() as db:
            row = db.execute('SELECT ' + ('1' if check else 'content') + ' FROM shares WHERE token_hash = ? AND expires > ?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        # 外壳策略升级同样影响旧快照；读取时按现行规则再过滤，不能只保护新建分享。
        return (row[0] if check else sanitize(row[0])) if row else None

    def revoke(self, share_id):
        with self.connect() as db:
            db.execute('DELETE FROM shares WHERE id = ?', (share_id,))


# 密钥只从片段送入请求体；既不进请求 URL，也不送进不可信 iframe。
SHELL_SCRIPT = """addEventListener('hashchange',()=>location.reload());
const token=location.hash.slice(1),frame=document.querySelector('iframe'),note=document.querySelector('p');
async function refresh(){
  try{
    const result=await fetch('/preview-share/content',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token,check:!!frame.srcdoc}),cache:'no-store',credentials:'omit'});
    if(!result.ok){frame.remove();note.hidden=false;note.textContent='分享不存在、已撤销或已过期';return;}
    if(result.status!==204)frame.srcdoc=await result.text();
    note.hidden=true;setTimeout(refresh,10000);
  }catch(error){note.hidden=false;note.textContent='读取失败，请刷新重试';}
}
refresh();"""


def response(text, *, status=200, policy=POLICY + '; sandbox'):
    return web.Response(text=text, status=status, content_type='text/html', headers={**HEADERS, 'Content-Security-Policy': policy})


async def body(request):
    if request.content_length and request.content_length > MAX_BYTES * 6 + 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BYTES * 6, actual_size=request.content_length)
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > MAX_BYTES * 6 + 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BYTES * 6, actual_size=len(data))
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(text='无效 JSON')
    if not isinstance(result, dict):
        raise web.HTTPBadRequest(text='需要 JSON 对象')
    return result


@web.middleware
async def share_headers(request, handler):
    if not (request.path.startswith('/api/preview-shares') or request.path.startswith('/preview-share')):
        return await handler(request)
    try:
        result = await handler(request)
    except web.HTTPException as error:
        error.headers.update(HEADERS)
        raise
    result.headers.update(HEADERS)
    return result


def register_routes(app):
    app.middlewares.append(share_headers)
    store = ShareStore(config.DATA_DIR / 'private' / 'preview-shares.sqlite3')

    async def create(request):
        denied = check_web_auth(request)
        if denied is not None:
            return denied
        from ..deployment_settings import share_origin

        try:
            origin = share_origin()
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=503, headers=HEADERS)
        source = (await body(request)).get('html')
        if not valid_source(source):
            return web.json_response({'error': 'HTML 为空或超过 200KB'}, status=400, headers=HEADERS)
        try:
            result = store.create(source)
        except ValueError as error:
            return web.json_response({'error': str(error)}, status=429, headers=HEADERS)
        result['url'] = origin + result['path']
        return web.json_response(result, headers=HEADERS)

    async def listing(request):
        denied = check_web_auth(request)
        if denied is not None:
            return denied
        store.cleanup()
        with store.connect() as db:
            rows = db.execute('SELECT id, expires FROM shares ORDER BY expires DESC').fetchall()
        return web.json_response({'shares': [{'id': row[0], 'expires': row[1]} for row in rows]}, headers=HEADERS)

    async def revoke(request):
        denied = check_web_auth(request)
        if denied is not None:
            return denied
        store.revoke(request.match_info['id'])
        return web.json_response({'ok': True}, headers=HEADERS)

    async def content(request):
        data = await body(request)
        check = data.get('check') is True
        result = store.read(data.get('token'), check=check)
        if check and result:
            return web.Response(status=204, headers=HEADERS)
        return response(result or '分享不存在、已撤销或已过期', status=200 if result else 404)

    async def shell(request):
        digest = base64.b64encode(hashlib.sha256(SHELL_SCRIPT.encode()).digest()).decode()
        # srcdoc 会继承外壳 CSP；内层允许 data: 也不能放宽外壳的 default-src 'none'。
        policy = "default-src 'none'; script-src 'sha256-" + digest + "'; connect-src 'self'; frame-src about:; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        return response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>限时静态预览</title><style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style><p>正在读取限时预览…</p><iframe sandbox="" referrerpolicy="no-referrer" title="静态预览"></iframe><script>' + SHELL_SCRIPT + '</script>', policy=policy)

    async def cleanup_task(app):
        import asyncio
        store.cleanup()
        async def clean():
            while True:
                await asyncio.sleep(3600)
                store.cleanup()
        task = asyncio.create_task(clean())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(cleanup_task)
    app.router.add_get('/api/preview-shares', listing)
    app.router.add_post('/api/preview-shares', create)
    app.router.add_delete('/api/preview-shares/{id}', revoke)
    app.router.add_get('/preview-share', shell)
    app.router.add_post('/preview-share/content', content)
