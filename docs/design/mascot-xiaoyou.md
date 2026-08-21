# 小幽 Xiaoyou · vococo 吉祥物设计规范

vococo 的形象吉祥物,替代原「象棋」时代遗留的皇冠棋子 logo(`vococo-mark.svg` 旧版)。
一缕活泼的橙色小火苗/幽灵,无嘴无手,靠眼睛和一双波浪小脚表达情绪——设定是「永远在听」。

探索过程与全部候选方案见交互展示页:https://pub.chjm.cc/vococo-mascots/ (含 12 套配色试验)。
**最终定色:蜜橙单色**,不采用多配色切换——展示页仅作探索记录,本文档才是定案规范。

## 1. 定色

| 角色 | 色值 | 用途 |
|---|---|---|
| 主体 body | `#FF8A26` | 身体主色 |
| 受光 highlight | `#FFC07A` | 左上角高光块 |
| 暗部 shade | `#F56F0E` | 右下角暗部块 |
| 眼线/五官 ink | `#3D2410` | 眼睛、思考点、Zzz |

单色橙在深底(`#181716` 系)和浅底(`#fff`/`#FAF6F1` 系)上均成立,**不做深浅版本切换**,
产品双主题(`data-theme="light"` / `prefers-color-scheme`)直接复用同一套颜色。

## 2. 像素网格系统

- 基底 24×28 网格,身体轮廓 `NEUTRAL` profile 定义肩宽→收腰→摆裙的尖顶轮廓
- 简化图标变体 14×16(用于 favicon / 16px 以下场景),由独立的 `iconShadow()` 派生,而非缩小版身体
- 所有状态帧从同一 `NEUTRAL` 轮廓 + 部件叠加(平移 `dy/dx`、缩放 `scale`、脚型 `feet`、眼型 `eyes`)派生,不单独画新角色
- 渲染实现:每格 1px 的 `<i>` 元素 + `box-shadow` 逐点定位,配合 `steps(1,end)` 关键帧做逐帧动画,零图片依赖、任意缩放不糊

引擎源码:`vococo/gateway/adapters/web_static/mascot.js`(生产精简版,来自展示页引擎)。

## 3. 状态库(8 种)

| 状态 key | 中文 | 帧数/周期 | 视觉要点 | 当前落地 |
|---|---|---|---|---|
| `idle` | 待机 | 6 帧 / 4.8s | 呼吸浮动 + 波浪脚交替 + 眨眼 | ✅ 新对话欢迎屏 |
| `listening` | 聆听 | 4 帧 / 1.0s | 身下 5 根音柱高低交替跳动 | 待接入(语音转写/通话) |
| `thinking` | 思考 | 3 帧 / 1.2s | 头顶三点轮流跳 | 待接入(AI 思考中状态行) |
| `busy` | 忙碌 | 3 帧 / 0.6s | 身体左右快速摇摆 | 待接入(AI 工作中状态行) |
| `done` | 雀跃 | 3 帧 / 0.9s | 蹲-跳-落,跳跃时身体拉伸变形 | 待接入(任务完成通知) |
| `err` | 蔫掉 | 2 帧 / 3.2s | 灰阶去饱和 + X 眼 + 下垂 | 待接入(失败/错误提示) |
| `sleep` | 睡觉 | 3 帧 / 3.6s | 闭眼 + Zzz 依次飘出 | 待接入(服务离线) |
| `icon` | 图标简化形 | 1 帧(静态) | 14×16 极简轮廓,无脚部细节 | ✅ favicon / PWA 图标 |

## 4. 组件用法

```html
<link rel="stylesheet" href="/mascot.css">
<script src="/mascot.js"></script>

<span class="voco-mascot" data-state="idle"></span>
```

- 缩放:容器上设置 `--s`(默认 4,1 = 1px/格),整数值像素边缘最清晰,非必要不用小数
- 切换状态:`el.dataset.state = 'listening'` 或 `VocoMascot.setState(el, 'listening')`
- 动态插入的节点需要挂载像素点时调用 `VocoMascot.mountAll(root)`

## 5. 资源清单

**生产图标**(`vococo/gateway/adapters/web_static/`,均已替换旧象棋 logo,`index.html`/`manifest.json` 内 `?v=` 已同步升级,`sw.js` 缓存桶已升版 `vococo-shell-v6`):

| 文件 | 尺寸 | 背景 | 用途 |
|---|---|---|---|
| `favicon.ico` | 16/32/48 | 透明 | 浏览器标签页 |
| `vococo-mark.svg` | 矢量 | 透明 | `<link rel="icon" type="image/svg+xml">` |
| `icon-192.png` | 192×192 | 实底 `#181716` | PWA 图标 |
| `icon-512.png` | 512×512 | 实底 `#181716` | PWA 图标 |
| `icon-maskable-512.png` | 512×512 | 实底 `#181716`,内容收在安全区 | Android 自适应图标 |
| `apple-touch-icon.png` | 180×180 | 实底 `#181716` | iOS 主屏图标 |
| `mascot.css` / `mascot.js` | — | — | 可复用组件(全 8 状态引擎) |

**设计源文件**(`docs/design/assets/mascot-xiaoyou/`):

| 文件 | 说明 |
|---|---|
| `xiaoyou-idle.png` | 待机帧高清透明底图(800px 高,最近邻放大,不失真) |
| `xiaoyou-icon.png` | 简化图标形高清透明底图 |
| `xiaoyou-icon.svg` | 简化图标形矢量源文件(与 `vococo-mark.svg` 同源) |

## 6. 使用规范

- **不改色**:蜜橙是唯一定案色,若要探索新主题色先在展示页 `pub.chjm.cc/vococo-mascots/` 验证,不要直接改生产色值
- **不做非整数缩放**:`--s` 尽量取整数,半像素缩放会让边缘发虚,丢失像素风格的锐利感
- **不单独重绘新姿势**:新增场景优先从已有 `frameShadow()` 参数组合(`dy/dx/scale/feet/eyes/bars/dots/zzz`)派生,保持全系列同一套骨架比例
- **图标场景用 `icon` 简化形,不用完整 `idle` 缩小**:完整轮廓在 16px 量级会糊成一团,简化形是专门为小尺寸设计的独立形状

## 7. 待办(后续场景接入,非本次范围)

按落位优先级:聆听(转写占位/通话声纹球)→ 思考/忙碌(`stream.js` 状态行)→ 雀跃(任务完成通知)→
蔫掉(失败提示气泡)→ 睡觉(离线横幅)。接入方式统一:插入 `<span class="voco-mascot" data-state="...">`,
无需额外图片资源。
