# 原生 PPT 操作，不设版式协议

## 运行环境

`技能目录` 指当前 SKILL.md 所在的实际绝对路径。脚本放用户工作区，不放技能目录。

- python-pptx 是 vococo 锁定依赖；使用 **vococo 安装环境的 Python**。源码安装的技能位于 `安装目录/vococo/plugin/skills/editable-pptx`，解释器通常是 `安装目录/.venv/bin/python`。先检查该文件及 `"$PY" -c 'import pptx; print(pptx.__version__)'`，不要假定系统 python3 已安装依赖。
- 非源码安装使用实际安装 vococo 的解释器；找不到就明确报告，不搜索凭据或擅自安装。调用过程中不改变 cwd 到源码目录。
- 后续示例假定 `PY=/绝对/安装目录/.venv/bin/python`，`SKILL=/绝对/技能目录`。

```sh
"$PY" "$SKILL/scripts/pptx_workspace.py" "$PWD/原稿.pptx" > "$PWD/对象-v1.json"
# 阅读对象信息后可按稳定ID只检查某页，不默认把整稿反复塞进上下文：
"$PY" "$SKILL/scripts/pptx_workspace.py" "$PWD/原稿.pptx" --slide-id 256
```

检查结果：页面 `slide_id`、对象 `shape_id`、名称、类型、图层、尺寸/位置（英寸）、旋转、段落/run字体（pt）、表格、图表数据、图片指纹、分组子对象和备注。
`index/layer` 从0开始；`slide_id` 不等于页码。组内是父坐标系，不能当页面坐标；主题继承字体可能为 null，应查母版或实际渲染，不擅自当默认字体。

## 新建：直接调用 python-pptx

把以下代码写到工作区 `author.py`，替换技能绝对路径与输出名，再用 `$PY author.py` 执行。

```python
import sys
sys.path.insert(0, "/绝对/技能目录/scripts")
from pptx_workspace import Presentation, save_new
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_CONNECTOR
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

p = Presentation()  # 也可 open_deck('已授权的参考.pptx')，不是仅新建
p.slide_width, p.slide_height = Inches(13.333333), Inches(7.5)
s = p.slides.add_slide(p.slide_layouts[6])  # 默认空白布局；导入稿先 inspect layouts
s.background.fill.solid()
s.background.fill.fore_color.rgb = RGBColor.from_string('040413')

shape = s.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.HEXAGON,
                          Inches(.7), Inches(2), Inches(2), Inches(2))
shape.name = '自定义构图节点'
shape.fill.solid()
shape.fill.fore_color.rgb = RGBColor.from_string('1677FF')
shape.line.fill.background()
text = s.shapes.add_textbox(Inches(3.2), Inches(.7), Inches(8.5), Inches(1.3))
run = text.text_frame.paragraphs[0].add_run()
run.text = '标题可以自由定位与换行\n不是固定模板'
run.font.name, run.font.size = 'Hiragino Sans GB', Pt(30)
run.font.color.rgb = RGBColor(255, 255, 255)
s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(2.8), Inches(3), Inches(5), Inches(3))
# 原生图片：s.shapes.add_picture('授权的本地图片.png', Inches(9), Inches(4), width=Inches(3))
# 可调整 picture.crop_left/crop_right/crop_top/crop_bottom，不重画真实界面。

chart_data = CategoryChartData()
chart_data.categories = ['甲', '乙', '丙']
chart_data.add_series('示例数据，非业务成效', [20, 35, 50])
s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(2.5),
                   Inches(6.8), Inches(3.8), chart_data)
s.notes_slide.notes_text_frame.text = '数字仅用于测试原生图表能力。'
print(save_new(p, '自由构图-v1.pptx'))
```

图形、连接线、文本与图表都是原生对象；任意数量、位置、尺寸、分栏、颜色和内容由 AI 决定，不要求 `layout` 或 `elements` 字段。更多能力直接查询安装库的 API/types，不应因本例没出现就宣称不支持。

## 修改现有稿件：按对象定位，不整稿重建

先查检查报告，再把其中的真实 ID 填入脚本。`256/4` 只是示意，不保证稿件里存在。

```python
from pptx_workspace import open_deck, save_new, slide_by_id, shape_by_id
from pptx_workspace import move_shape, move_slide, remove_shape, remove_slide
from pptx.util import Inches
from pptx.chart.data import CategoryChartData

p = open_deck('原稿.pptx')
s = slide_by_id(p, 256)
shape = shape_by_id(s, 4)
# 保留字体和段落格式；替换全部 text_frame.text 会丢失富文本。
shape.text_frame.paragraphs[0].runs[0].text = '修改后的实际文字'
shape.left, shape.top = Inches(.8), Inches(1.2)
shape.width = Inches(10.5)
# 表格：shape_by_id(s, 实际ID).table.cell(1, 0).text = '新内容'
# 图表：chart = shape_by_id(s, 实际ID).chart
# data = CategoryChartData(); data.categories = ['甲', '乙']
# data.add_series('更新后', [30, 60]); chart.replace_data(data)
move_shape(s, shape.shape_id, len(s.shapes) - 1)  # 置于同组顶层；组内索引用组内对象数
move_slide(p, s.slide_id, len(p.slides) - 1)
# 增页：p.slides.add_slide(p.slide_layouts[选中的布局索引])
# 删对象：remove_shape(s, 实际ID)
# 删页：remove_slide(p, 实际slide_id)，拒删最后一页/被跳转引用的页/复杂导航
save_new(p, '改稿-v2.pptx')
```

辅助函数使用锁定版本的部分内部 XML 接口操作页序/图层，有真实保存重开回归。删除对象不会主动清理孤立媒体；不承诺缩小文件。
对不受库直接支持的能力，可在副本上检查 OOXML 并精准修改、验证关系和重渲染；不要直接丢弃对象或把整页重新截图。

## 新建的另一种方式：完整 PptxGenJS API

```javascript
const fs = require('node:fs');
const {library} = require('/绝对/技能目录/scripts/generate.cjs');
const PptxGenJS = library();
const p = new PptxGenJS();
p.layout = 'LAYOUT_WIDE';
const s = p.addSlide();
s.background = {color: '040413'};
s.addText([{text:'原生富文本',options:{bold:true}}, {text:' / 自主构图'}],
          {x:.8,y:.7,w:11,h:1,fontFace:'Hiragino Sans GB',fontSize:32,color:'FFFFFF'});
s.addShape(p.ShapeType.chevron, {x:1,y:3,w:3,h:1.5,fill:{color:'1677FF'}});
// p.ShapeType / p.ChartType / addImage / addTable / addNotes 等完整 API 可直接用。
(async () => {
  const bytes = await p.write({outputType:'nodebuffer'});
  fs.writeFileSync('native-v1.pptx', bytes, {flag:'wx'});
})().catch(error => {console.error(error); process.exitCode=1;});
```

PptxGenJS 是新建库，不把它当已有 PPT 读取器。旧 JSON 生成器与完整 API 共用已锁定缓存；缓存缺失交给维护者审批运行 setup.py，不在生成脚本里联网安装。

## 实际预览与迭代

```sh
"$PY" "$SKILL/scripts/check.py" "$PWD/改稿-v2.pptx" --render "$PWD/预览-v2"
```

`预览-v2` 必须是新目录；返回实际 PDF、按数值页序排列的 PNG 路径和 `renderer=LibreOffice`。输入不能引用自动加载的外部图片/资源；先授权取得并嵌入资源。超链接可保留。
渲染器使用临时 profile 禁宏，并只打开原稿副本；这不是独立系统沙箱，仍继承宿主现有权限。

1. Read 逐页 PNG，或先拼接这些真实 PNG 的总览（不是另画版面）。
2. 指出并修复实际问题，例如标题断行、节点遮挡、图表文字难读。
3. 另存 v3 并重渲染，最终只交付最后验过的版本。
4. 视觉回执必须说明实际看过哪些页、哪些页未看；不能把 `rendered_needs_human_or_model_review` 读成视觉自动通过。

维护者安装：python-pptx 随 `uv sync` 安装；LibreOffice 来自 libreoffice.org 或官方包管理器，Poppler 提供 pdftoppm/pdfinfo；三者都不能由业务任务静默安装。
