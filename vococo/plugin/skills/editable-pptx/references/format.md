# 内容 JSON

顶层：`title`（必填，120 字符内）、`author`（可选）、`theme`（可选）、`slides`（1–50 页）。
`theme` 可设置 `font`、`background`、`ink`、`accent`；颜色用无 # 的六位 RGB。
默认 Microsoft YaHei；目标电脑缺少此字体时可能替换，按实际办公环境选字体。

每页共同字段：`layout`、`title`（最多 32 字符）、`notes`（可选，4000 字符以内）。
显示文字不得包含换行/制表符，避免人为断行突破版式；备注允许多行。字段不支持时会报错，不静默丢弃。

| layout | 专属字段 | 限制 |
|---|---|---|
| cover | subtitle（可选） | 副标题 ≤72 字符 |
| bullets | items | 1–5 条，每条 ≤54 字符 |
| columns | columns: [{title, items}, {title, items}] | 固定两栏，栏标题 ≤18 字符，每栏 1–5 条、每条 ≤28 字符 |
| table | headers、rows | 2–5 列、1–7 行数据；表头/每格 ≤16 字符，行列数一致；数值先转文字 |
| chart | chartType、labels、series | bar/line/pie；2–8 类目，每个标签 ≤12 字符；1–4 个系列（pie 仅1个） |
| image | path、caption（可选） | path 是 JSON 同目录/子目录本地 PNG/JPEG，≤20MB；说明 ≤72 字符 |

图表 `series` 为 `[{"name":"销售额", "values":[12,20,18]}]`；系列名 ≤18 字符。
数值个数等于 labels 个数，必须为有限数，绝对值 ≤1e12；饼图不能含负数且至少一个正数。
图表通过原生 chart 节点及嵌入 XLSX 保存数据，不用截图替代。

```json
{
  "title": "季度汇报",
  "slides": [
    {"layout": "cover", "title": "季度汇报", "subtitle": "面向管理团队"},
    {"layout": "chart", "title": "销售额（示例数据）", "chartType": "bar",
     "labels": ["一月", "二月", "三月"], "series": [{"name": "销售额", "values": [12,20,18]}]}
  ]
}
```

首期使用保守版式，仍不能替代 Office 渲染检查。复杂长内容应拆页，而不是突破限制。
