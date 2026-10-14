---
name: vococo-web-publish
description: >-
  发布网页或 demo 并自动收录作品库。用户说“发布网页”“给我页面链接”“部署应用”时触发。
  必须调用统一 publish_website 工具，不能手工 rsync/SSH 绕过审批及作品登记。
  静态页支持 kimmy 公网发布及 local 本机激活；site 和 Node/Python 尚未支持，必须如实说明。
---

# 网页发布统一入口

## 操作顺序

1. 先完成页面，准备只包含公开静态产物的目录，根目录须有 `index.html`。
   不要传整个仓库、家目录、软链接、`.git`、`.env`、依赖目录或任何私密文件。
2. 用户明确发布目标；不要把“公网发布”擅自降为本地发布。
3. 调用 `mcp__vococo__publish_website`，示例参数：

```json
{
  "target": "kimmy",
  "slug": "product-intro",
  "source_dir": "/绝对路径/公开静态产物",
  "title": "产品介绍",
  "type": "static"
}
```

4. 工具会先走内置危险审批，再激活站点并自动登记作品库。不需要再调用独立收藏步骤。
5. 按回执回复：`activation=succeeded` 表示部署激活，`http_verification=verified` 才表示 HTTP 200 验活。
   Kimmy 固定发布到 `https://pub.chjm.cc/<slug>/`，验活超时/非200表示“已部署、访问待验证”，不要重新部署。
   `local` 返回回环 URL，仅表示本机文件激活、未验活；`/pub/` 本身不要求登录，若绑定外网或有公网隧道，
   站点可能已对外公开。`local` 不是私密空间，也不可擅自用来替代用户要求的公网发布。

## 更新与安全边界

- 同一目标和 slug 更新同一作品。工具保留上一版文件。
- 发现已有发布目录含未登记的改动，或并非此发布器管理时，拒绝覆盖。
  先保留/核对现有内容，或经用户确认使用新 slug；禁止为了继续发布盲目 `--delete`。
- Kimmy 使用固定 SSH 别名 `Kimmy`，原 `/opt/deploy/deploy.sh <slug> static`，不更换系统。
  工具检查远端脚本指纹/配置、git未提交与未跟踪改动、incoming/在线目录/配置片段是否偏离登记快照。
  首次接管已有站点仅当 incoming 与在线文件一致才允许；不一致时拒绝覆盖，不猜哪边是最新版。
- 上传只进全新临时目录；上传摘要必须符合审批快照；激活前备份 incoming、在线文件及 nginx片段，
  再调用原脚本。原脚本内部的 `--delete` 仅在这些保护完成后运行。备份路径见回执。
- 回执 `activation=unknown` 表示 SSH/脚本中断，可能已修改线上文件；先核对备份和状态，禁止盲目重发。
- `site`、`node`、`python` 返回“未支持”。site现有脚本清理旧release且没有未登记改动检测，
  不能保证安全包装；两套域名/根目录/来源键不混用，也不能转为手工发布绕过工具。
- 撤下网站尚无统一工具，不要把“隐藏作品”解释为撤下公开网站。

## 收录失败只补记账

若回执显示 `library_status=failed`，站点已经激活，**不要重新发布**。
将回执 `retry` 对象原样作为 `mcp__vococo__collect_library_item` 的参数，保留
`source_key`、`event_key`、来源会话/轮次及 metadata。成功会返回 `library_id`。
若依然失败，报告“发布成功、收录待重试”，保留回执，不静默吞掉错误。

检索或复用已有作品使用 `search_library` / `get_library_item`，不要扫描聊天 URL 猜作品。
