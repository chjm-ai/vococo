---
name: hermes-web-publish
description: >-
  把一个网页发布到 Cloud Hermes（Wazir）已经配置好的公网域名 wazir.example.com 下——只需要往
  claude-hermes 主仓库的 data/published/ 目录丢一个文件，就能通过
  https://wazir.example.com/pub/<文件名> 直接公网访问，不用新起服务、不用配置新域名、不用重启
  进程。当用户说"把这个网页发布一下""这个页面能不能发个链接给我在手机上打开""挂到网上看
  看""发布这个 HTML/demo"，且当前工作目录是 claude-hermes 主仓库（不是某个编码任务的
  git worktree）时触发。不要用于：部署到别的服务器（那是 website-publisher，走 SSH/kimmy）、
  发布到飞书妙搭（那是 lark-apps）、生成网页本身的设计（先用 frontend-design 把页面做出
  来，做完再用本 skill 发布）。
---

# Cloud Hermes 网页发布

Cloud Hermes（内部代号 Wazir）本身就是一个常驻公网的 Web 服务：cloudflared 隧道把
`wazir.example.com` 转发到本机 `localhost:8848`（`~/.cloudflared/config.yml`）。这个服务
已经开了一条公开静态路由 `GET /pub/{path}`，直接读 claude-hermes 仓库下的
`data/published/` 目录——**发布=往那个目录写一个文件，不用碰代码、不用重启。**

对应实现：`claude_hermes/gateway/adapters/web.py` 的 `_handle_publish` /
`_safe_published_path`，路由表里的 `web.get("/pub/{path:.*}", ...)`；目录路径见
`config.PUBLISHED_DIR`（= `data/published/`）。

本 skill 打包在 `claude_hermes/plugin/`（本地 SDK 插件 `hermes-internal`）里，只在
claude-hermes 自己的 Hermes 会话中加载——Claude Code / Codex / OpenCode 等其它工具看
不到它，不用管跨工具共享的顾虑。

## 前置检查（先做,别跳）

1. **确认自己不在 worktree 里。** `pwd` 应该是 `/Users/wesley/Repos/claude-hermes`
   本身，而不是 `.../data/worktrees/<hash>/<slug>` 这种路径。日常聊天/任务对话本来就是
   在主仓库根目录跑，天然满足；但如果这轮明显是一个"改代码"的编码任务（被派进了独立
   worktree），**不要**在那里写 `data/published/`——worktree 里没有这份 `data/`
   目录，各 worktree 会各自建一份互不相通的空目录，文件会安安静静地"发布成功"但外网
   完全打不开（服务进程读的是主仓库那份 `data/`，不是 worktree 里新建的那份）。遇到这
   种情况就告诉用户"这个要在正常对话里发，不是编码任务"，等编码任务收尾、回到主仓库
   对话再发布。
2. 确认服务确实开着(通常是的,常驻进程):`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/` 应该拿到 `200`。拿不到就不是本 skill 能处理的问题(找 [[hermes-serve-restart]] 类操作),先如实告诉用户。

## 发布步骤

1. 想好一个干净的文件名/路径,只用英文小写、数字、短横线,带上正确扩展名,例如
   `demo.html`、`logo-preview.html`、`reports/q3.html`。**别用中文名/空格**——虽然
   `curl`/服务器不会挂,但公网链接里带中文很容易被聊天软件/浏览器地址栏吞掉或转义得很难看。
2. 用 Write 工具直接写到 `data/published/<你选的路径>`(相对主仓库根目录的相对路径即
   可,不需要绝对路径,也不需要事先 `mkdir`——父目录不存在时用 Bash `mkdir -p` 建一下,
   或者直接用 Write 工具,它自己会建父目录)。
   - 单文件页面:`data/published/demo.html`——发布 HTML/内联 SVG/内联 `<style>` 都行,
     跟 Artifact 工具同一个自包含原则:样式/脚本内联进这一个文件,**不要指望加载外部
     CDN——CSP 挡了**(见下面"限制"),需要图片就转 data URI 内嵌。
   - 多文件小站点:`data/published/mysite/index.html` + `data/published/mysite/style.css`
     之类,访问 `.../pub/mysite/` 会自动回退到该目录下的 `index.html`,子文件按各自
     文件名访问(`.../pub/mysite/style.css`)。
3. 本地过一遍(不依赖外网/隧道,更快):
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/pub/<路径>`
   应该拿到 `200`。拿到 `404` 多半是路径打错或文件没落盘,回头检查。
4. 把公网链接发给用户:`https://wazir.example.com/pub/<路径>`。到这一步就完事了——不用
   `git add`/`commit`、不用碰 `merge-main.sh`、不用 `restart_self`。这些是"改代码"才
   需要的流程,发布静态页面完全绕开它们。

## 撤下 / 更新

- 撤下:直接删掉 `data/published/` 下对应文件(`rm data/published/demo.html`),链接立刻
  404,不用重启。
- 更新内容:直接覆盖写同一个文件即可,响应带 `Cache-Control: no-cache`,浏览器/CDN 不
  会缓存旧版本,刷新页面就看到新内容。

## 限制(设计上就是这样,不是 bug)

- **无鉴权,谁有链接谁能看。** `/pub/*` 不走 `X-Auth-Token` 校验——这条路由的存在意义
  就是给没登录的人一个能打开的公开链接。别把不想公开的内容(截图里带真实数据、内部文档)
  发到这里;真要公开访问受限内容,先跟用户确认。
- **CSP 沙箱隔离,不是不安全,反而是故意收紧的。** `/pub/*` 的响应比站点其它页面多一条
  `sandbox` CSP 指令,把发布页强制放进浏览器的"不透明源":页面内联 `<script>` 照样能跑,
  但读不到 Wazir 聊天主界面的 `localStorage`/登录态(即便发布页内容有问题也偷不走访问
  口令),也没法用 `<form>`/`fetch` 悄悄往 `/send` 之类的接口发状态变更请求。日常发静态
  demo/预览页完全不受影响,只有"想让发布页反过来操控聊天后台"这种用法会被挡,而这本来
  就不该是发布页的职责。
- **只能挂静态文件,不能跑后端逻辑。** 想要的是服务器渲染/接 API 后端,这条路由做不到,
  该走别的方案(比如让用户明确要新起一个服务,那是另一件事,不在本 skill 范围)。
- **不做目录穿越、不认隐藏文件。** 路径解析会拒绝 `..` 越界和任何以 `.` 开头的路径段,
  正常发布不会撞到这个限制,只是别指望能拿它当通用文件浏览器用。

## 例子

用户说"帮我做个简单的产品介绍页,发个链接我用手机看看":
1. 用 frontend-design 把 `demo.html` 写出来(自包含单文件,内联 CSS)。
2. Write 到 `data/published/product-intro.html`。
3. `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/pub/product-intro.html` → `200`。
4. 回复:「发布好了:https://wazir.example.com/pub/product-intro.html」
