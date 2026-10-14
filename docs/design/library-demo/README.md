# 作品库 Library · 独立 Demo

本轮仅用于界面与交互确认，不接入正式应用，不扫描真实文件，不读取会话、不做自动收录。所有初始作品及封面都是示例；改动只在当前页面内存中有效，刷新重置。

## 查看

用浏览器打开本目录 `index.html`。不需要启动服务器或安装应用依赖。

- `base.css`：从现有前端复制的主题令牌、侧栏壳与导航行快照。
- `demo.css` / `demo.js`：独立布局、示意封面与内存交互。
- `check.cjs`：Playwright 浏览器验收，并打包单文件 HTML。

## 验证与打包

使用本机已安装的全局 Playwright：

```sh
NODE_PATH=/usr/local/lib/node_modules node docs/design/library-demo/check.cjs
```

输出到当前 worktree 的 `data/library-demo/`（不纳入 Git）：

- `library-demo.html`：可单独拷走并直接打开的完整 Demo。
- `desktop.png` / `mobile.png` / `detail.png` / `dark.png`：界面截图。

验证范围：筛选、搜索、空状态、排序、置顶、隐藏恢复与撤销、详情与引用演示、链接协议校验与输入转义、复制失败回退、模拟收录、刷新重置、320/375/390/768/1280 屏宽、深色主题、无 JS 错误及无 HTTP 请求。

## 确认后再做

真实产出自动收录、历史内容回填、文件与网站真实预览、来源会话跳转、引用到真实对话、持久化与访问权限。隐藏是整理视图，不是删除文件，也不是访问权限控制。
