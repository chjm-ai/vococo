---
name: video-assets
description: >-
  视频素材库管理——下载、入库、搜索、浏览视频素材。当用户说
  "下载这个视频到素材库""找一下 jev 相关的素材""素材库里有什么""加几个素材进来"
  "删掉这条素材""素材库概况""批量下载这些链接"时触发。
  也覆盖"收集 XX 的视频素材""去搜一下 XX 的素材"等表达。
  不负责视频剪辑和拼接（那是后续剪辑 skill 的事）。
---

# 视频素材库管理

## 库位置与结构

```
~/Media/video-assets/
├── index.json          # 主索引（所有元数据）
└── clips/              # 所有素材平铺存放
```

管理脚本: `vococo/plugin/skills/video-assets/scripts/manage.py`

## 操作手册

### 入库单条素材

```bash
python3 vococo/plugin/skills/video-assets/scripts/manage.py add "<URL>" \
  --tags "tag1,tag2,tag3" \
  --source youtube \
  --name "自定义文件名" \
  --notes "素材描述"
```

- `--source` 可选值: `official`（品牌官方）、`youtube`、`pexels`、`pixabay`、`original`（自制）
- `--name` 可选，不填则用视频原标题
- 入库时自动用 ffprobe 提取 duration/resolution/fps/size
- 自动按 source_url 去重，不会重复下载

### 批量入库

准备一个文本文件，每行格式: `URL\ttags\tnotes`（tab 分隔，tags 和 notes 可省略）

```bash
python3 vococo/plugin/skills/video-assets/scripts/manage.py batch urls.txt --source youtube
```

### 搜索素材

```bash
# 按标签
python3 vococo/plugin/skills/video-assets/scripts/manage.py search --tag jev

# 按来源
python3 vococo/plugin/skills/video-assets/scripts/manage.py search --source official

# 按时长范围（秒）
python3 vococo/plugin/skills/video-assets/scripts/manage.py search --min-dur 30 --max-dur 120

# 按关键词（搜 tags + notes + 文件名）
python3 vococo/plugin/skills/video-assets/scripts/manage.py search --keyword "demo"

# 组合筛选
python3 vococo/plugin/skills/video-assets/scripts/manage.py search --tag jev --max-dur 600
```

### 浏览库存

```bash
# 列出所有素材
python3 vococo/plugin/skills/video-assets/scripts/manage.py list

# 概况统计（总数/总时长/大小/来源分布/热门标签）
python3 vococo/plugin/skills/video-assets/scripts/manage.py list --summary
```

### 查看详情

```bash
python3 vococo/plugin/skills/video-assets/scripts/manage.py info v0001
```

### 删除素材

```bash
python3 vococo/plugin/skills/video-assets/scripts/manage.py remove v0001
```

同时删除文件和索引记录。

## 命名规范

文件命名: `{来源}-{关键词}-{序号或描述}.mp4`

例:
- `jev-official-launch-demo.mp4`
- `pexels-tech-circuit-board-4821.mp4`
- `original-screen-record-jev-api.mp4`

## 元数据字段说明

| 字段 | 说明 |
|---|---|
| id | 自增编号 v0001, v0002... |
| file | 相对于库根目录的路径 |
| source_url | 原始下载链接（用于去重） |
| source | official / youtube / pexels / pixabay / original |
| copyright | brand-promo / fair-use / cc0 / cc-by / original |
| tags | 标签数组，用于搜索 |
| duration | 秒数（ffprobe 自动提取） |
| resolution | 如 1920x1080（ffprobe 自动提取） |
| fps | 帧率（ffprobe 自动提取） |
| size_mb | 文件大小 MB（ffprobe 自动提取） |
| added | 入库日期 |
| notes | 自由备注 |

## 搜索素材的工作流

当用户要求搜集某主题的素材时:
1. 用 WebSearch 搜索相关视频（官方渠道 + YouTube + B站）
2. 整理出候选列表给用户确认
3. 确认后用 `add` 或 `batch` 命令入库
4. 入库完成后用 `list --summary` 报告库存概况

## 注意事项

- yt-dlp 下载限制 1080p，避免文件过大
- 超过 30 分钟的视频（如直播录像）通常不适合当素材，入库前提醒用户
- 竖屏视频（如抖音/Shorts）照常入库，在 notes 里标注「竖屏」
- Pexels/Pixabay 的素材可直接用 URL 下载，source 标为对应平台，copyright 标为 cc0
