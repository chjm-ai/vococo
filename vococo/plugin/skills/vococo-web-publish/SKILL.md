---
name: vococo-web-publish
description: >-
  把网页或带后端的应用发布到公网。支持三种类型：静态页面（丢文件即发）、Node 后端
  （npm 依赖+systemd+nginx 反代）、Python 后端（venv+gunicorn 同样）。当用户说
  "把这个网页发布一下""这个页面发个链接给我""挂到网上看看""发布这个 HTML/demo"或
  "把这个小应用部署上去"时触发。不要用于：生成网页本身的设计（先用 frontend-design/
  web-design 把页面做出来，做完再用本 skill 发布）。
---

# 网页/应用发布

> **这是一份模板，需要你先配好自己的发布通道**：一台有公网的服务器（ssh 别名
> 下文写作 `<pub-host>`）+ 一个指向它的发布域名（下文写作 `pub.example.com`）
> + 服务器上的部署目录约定（下文写作 `/opt/deploy/`，内含你自己的 `deploy.sh`
> 负责装依赖、起服务、配 nginx 反代）。把这三处换成你的实际值即可。

## 发布步骤（三步）

### 1. 命名 + 识别类型
- 应用名：英文小写、短横线，如 `food-log`、`invoice-helper`。**别用中文/空格**。
- 看项目根目录判断类型：
  - 只有 html/css/js → `static`（零依赖，丢文件即发布）
  - 有 `package.json` → `node`（默认启动文件 `server.js`，可指定其他）
  - 有 `requirements.txt` / `pyproject.toml` → `python`（默认 gunicorn `app:app`）

### 2. 上传 + 部署（一行命令对）
```bash
# 上传（排除依赖目录，服务器现场安装）
rsync -az --exclude node_modules --exclude .venv --exclude .git --exclude __pycache__ <本地目录>/ <pub-host>:/opt/deploy/incoming/<应用名>/
# 部署（静态不需要端口）
ssh <pub-host> '/opt/deploy/deploy.sh <应用名> static'
ssh <pub-host> '/opt/deploy/deploy.sh <应用名> node <端口> <启动文件>'
ssh <pub-host> '/opt/deploy/deploy.sh <应用名> python <端口> <模块:应用>'
```

**端口分配**：固定一个段（如 3100-3199）；发布前先查占用：
`ssh <pub-host> 'ss -tln | grep :31'`。后端端口从段内最小空闲起用。

### 3. 验证 + 给链接
```bash
curl -s -o /dev/null -w '%{http_code}\n' https://pub.example.com/<应用名>/
# 200 → 回复：https://pub.example.com/<应用名>/
```

## 静态页快速发布（最常见场景）
单文件/小目录不需要搞端口，就是两步：
```bash
mkdir -p /tmp/pub-<app> && cp 页面文件 /tmp/pub-<app>/index.html
rsync -az /tmp/pub-<app>/ <pub-host>:/opt/deploy/incoming/<app>/
ssh <pub-host> '/opt/deploy/deploy.sh <app> static'
```
多文件站点同理（index.html + 子文件一起 rsync，`<应用名>/` 会自动回退 index.html）。

## 撤下 / 更新
- **更新**：重复上面"上传+部署"即可，nginx reload 零中断。
- **撤下**：停掉对应 systemd 服务、移走 nginx snippet 与应用目录、`nginx -t` 后 reload。

## 注意事项（踩过的坑，别再来一次）
1. **上传前先查目标目录有没有"本地没有的更新版本"**——曾用本地旧版覆盖过服务器上
   更新的版本。rsync 前先 `ssh <pub-host> 'ls /opt/deploy/incoming/<app>/ /var/www/<app>/ 2>/dev/null'` 确认。
2. **CDN Full 模式回源走 443**：nginx 必须监听 443 + 配置证书。只配 80 会外网 404。
3. **后端要监听 127.0.0.1 或 0.0.0.0 均可**（nginx 反代走内网），但端口必须和 deploy.sh 参数一致。
4. **Node 依赖装一次**：deploy.sh 现场 `npm install`，重复发布自动跳过已装依赖；代码更新只重传源码。

## 例子
用户说"帮我做个产品介绍页，发个链接我用手机看看"：
1. frontend-design 生成自包含 `index.html`。
2. `mkdir -p /tmp/pub-intro && cp index.html /tmp/pub-intro/`
3. `rsync -az /tmp/pub-intro/ <pub-host>:/opt/deploy/incoming/intro/ && ssh <pub-host> '/opt/deploy/deploy.sh intro static'`
4. `curl -s -o /dev/null -w '%{http_code}' https://pub.example.com/intro/` → 200
5. 回复：「发布好了：https://pub.example.com/intro/」

用户说"这个小应用要带后端，帮我发一下"：
1. 识别 `package.json` → node 类型。
2. 端口查空：`ssh <pub-host> 'ss -tln | grep :31'` → 3101 空闲。
3. 上传 + `deploy.sh <app> node 3101 server.js`。
4. 验证 200 → 给链接。
