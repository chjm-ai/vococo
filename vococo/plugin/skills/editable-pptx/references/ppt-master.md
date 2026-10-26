# PPT Master 接入

## 上游与安装

- 官方仓库：https://github.com/hugohe3/ppt-master
- 包目录：`skills/ppt-master/`，MIT；完整保留 LICENSE、版权、SPONSORS 文件及内部引用，不抄写、删减或重造其设计方法。
- 本次核验版本：`6.7.3`，提交 `78759bf62e9a1ef43e926ff9e50afa4cfe9a63cb`。更新后须重新核验，版本号不是质量保证。
- 共享安装：`~/Desktop/Repos/AI_Skills/ppt-master/`。可通过 `VOCOCO_PPT_MASTER_DIR` 显式指定另一完整安装；该变量是路径，不是安装或外发授权。
- 本仓库只带适配层，不捆绑约 78MB 的上游素材和脚本。没有安装的机器应明确报缺失，由维护者从官方固定提交安装；业务会话不临时联网下载。

先用 vococo 安装环境的 Python 执行适配层校验（先按 [原生操作](native-api.md) 解析 `$PY`）：

```sh
"$PY" "适配技能绝对目录/scripts/ppt_master_check.py"
```

此命令解析 `VOCOCO_PPT_MASTER_DIR` 或共享默认目录，核对完整包的文件名/源码 SHA-256 聚合指纹，再运行上游 guard，成功才输出绝对 `SKILL.md` 路径。它不联网、不更新，忽略运行时 `__pycache__`/`.pyc`，不是沙箱或第三方安全认证。上游 guard 本身只验发行身份，不能证明提交版本；聚合指纹才负责本次固定源码约束。包被改动、增删文件或换版均失败，维护者重新评估后才能更新指纹，不由业务代理自动接受。

校验成功后才调用/Read 上游入口，以该绝对目录作为 `SKILL_DIR`，按 `workflows/routing.md` 选择路线。缺失或校验失败停止，不修补、不绕过、不切换自建生成器。普通生成保留其策略确认；用户明确要求快速/跳过策略时才走 Quick。评估中的三页短稿不能当作完整 Default 工作流验收。

## 环境与权限

- 核心原生导出可复用 vococo 已安装的 Python 环境（含 python-pptx/Pillow）；先按 [原生操作](native-api.md) 解析解释器，不假定系统 python3 已装依赖。运行上游命令时用此解释器替换示例中的 python3。
- 不整包安装上游 requirements：生图、TTS、PDF、布尔形状、浏览器服务各有可选依赖，只在实际触发且获得审批后安装。PyMuPDF 另有 AGPL/商业许可，不能当 MIT 依赖随便打包。
- 客户材料默认仅在本机工作区处理；外部生图、联网搜图、TTS 或研究会外发提示词/材料，必须先明确发送内容并获授权。真实产品截图不交生图重画。
- 不自动运行 `update_repo.py`、pip、浏览器安装、后台/常驻本地编辑器或确认服务器。上游确认需要本地网页而当前渠道打不开时，停在关口说明限制，不伪造确认，不擅自改用 Quick。
- 上游脚本不是独立沙箱。当前工作目录/正式 output_dir、工具权限与审批仍适用，不能借上游脚本访问越权目录。
- 原稿只读复制进工作区；每次导出指定尚不存在的新版本路径，不覆盖既有 PPTX 或用户原稿。上游命令的默认备份不代替这一要求。

## 验收与交付

1. 上游完成设计、源 SVG、质量检查和原生 PPTX 导出；图表/表格需要数据级编辑时按上游 native-data 契约及 `--native-charts-and-tables`，不能把可编辑形状误称为原生数据图表。
2. 上游浏览器预览通常渲染 SVG，只能证明源画面；必须再用适配层 `scripts/check.py 最终.pptx --render 新预览目录` 渲染最终 PPTX。
3. Read 查看实际 PNG，修复源稿再导出新版本并重渲染。不能用 SVG 截图替代这一步。
4. Web 使用 `mcp__vococo__send_file` 私有交付；需要展示时发送实际 PPT 渲染图，不公开发布客户材料。
5. 清楚区分结构检查、LibreOffice 渲染、AI 视觉复核与 PowerPoint/WPS 人工编辑验收。没有最后一项就不能宣称 Office 全面兼容；图像内部仍不可逐元素编辑。
