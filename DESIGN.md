# DESIGN.md — vococo Web 设计令牌规范

前端样式的唯一真源在 `vococo/gateway/adapters/web_static/styles.css` 顶部的 `:root` 块。
**写新样式一律用下表的 CSS 变量,不要再手写颜色值、字号、间距、圆角、过渡时长。**

风格基调:Claude Code 暖炭灰 + 赤陶橙,深色为默认,浅色跟随系统或手动固定。

---

## 一、令牌总表

### 1. 尺寸类(与主题无关,只有一套)

| 类别 | 令牌 | 值 | 用途 |
|---|---|---|---|
| 字号 | `--fs-sm` | 12px | 元信息、标签、次要说明(**全站字号下限**) |
| | `--fs-md` | 13px | 密集列表、按钮内文字 |
| | `--fs-base` | 14px | 正文默认 |
| | `--fs-lg` | 15px | 强调正文、小标题 |
| | `--fs-xl` | 16px | 区块标题 |
| 间距 | `--sp-1` `--sp-2` `--sp-3` `--sp-4` `--sp-5` | 4 / 6 / 8 / 12 / 16px | padding、gap |
| 圆角 | `--r-sm` `--r-md` `--r-lg` `--r-xl` | 6 / 9 / 12 / 16px | 小控件 → 大卡片 |
| | `--r-pill` | 999px | 胶囊、徽标 |
| 过渡 | `--t-fast` | .15s | 颜色/背景等交互反馈 |
| | `--t-base` | .22s | 位移、展开收起 |

**下限规则**:字号不得低于 12px(移动端读不清),原有 9/10/10.5/11/11.5px 已统一上调。
**例外**:20px 以上的大标题、`50%` 圆形、`0` 直角、`1~2px` 微调间距不套令牌,按需直写。

### 2. 颜色类(深浅双主题各一套值)

**基础色**(原有,共 41 个):`--bg` `--bg2` `--panel` `--panel2` `--card` `--line` `--line2`
`--text` `--dim` `--dim2` `--accent` `--accent2` `--user1` `--user2` `--code-bg` `--code-fg`
`--scroll` `--shadow` `--focus-ring` `--glow1` `--glow2` 等。

**语义状态色**:

| 令牌 | 用途 |
|---|---|
| `--ok` / `--ok-fg` / `--ok-line` / `--ok-bg` | 成功、在线、已完成 —— 主色 / 前景文字 / 边框 / 底色 |
| `--err` / `--err-fg` / `--err-line` / `--err-bg` | 错误、离线、阻塞 —— 同上四件套 |
| `--warn` | 警告、连接中、待处理 |
| `--danger` / `--danger2` | 停止/销毁类按钮的渐变。**比 `--err` 更红更醒目**,不要互相替代 |
| `--on-accent` | 强调色或任何彩色底上的前景文字(取代散落的 `color:#fff`) |

**专用色**(刻意不并入语义色,原因见第三节):

| 令牌 | 用途 |
|---|---|
| `--diff-add` / `--diff-del` | 代码差异增/删,沿用 GitHub 标准值 |
| `--tag-sun` `--tag-copper` `--tag-leaf` `--tag-ink` `--tag-olive` `--tag-gold` | 项目/任务分类标签,互相区分用 |

**遮罩与叠加**(半透明,深浅主题通用):

| 令牌 | 值 | 用途 |
|---|---|---|
| `--ov-3` | rgba(0,0,0,.5) | **所有模态弹窗遮罩的标准值** |
| `--ov-4` | rgba(0,0,0,.88) | 图片查看器等需要更暗的全屏遮罩 |
| `--ov-1` `--ov-2` | .12 / .32 | 轻/中度黑色叠加 |
| `--wh-1` `--wh-2` `--wh-3` | .14 / .28 / .85 | 深色底上的白色叠加(按钮底、hover、次要文字) |

---

## 二、怎么用

```css
/* ✅ 正确 */
.my-btn{
  padding:var(--sp-3) var(--sp-4);
  font-size:var(--fs-md);
  border-radius:var(--r-md);
  background:var(--panel);
  color:var(--text);
  transition:background var(--t-fast),color var(--t-fast);
}
.my-btn:hover{background:var(--panel2)}
.my-btn.is-error{color:var(--err)}

/* ❌ 错误:手写数值 */
.my-btn{padding:9px 11px;font-size:13.5px;border-radius:10px;transition:background .16s}
```

**加新颜色前先问**:能不能用现有语义色表达?只有确实是新语义(而非新色号)时才加令牌,
且必须在**三个主题块都补上**——`:root`(深色)、`@media(prefers-color-scheme:light)`、
`:root[data-theme="light"]`。只加一处会导致另一主题下颜色缺失或刺眼。

---

## 三、刻意不收敛的部分(别"顺手"合并)

| 项 | 为什么保留 |
|---|---|
| `--diff-add/--diff-del` 不并入 `--ok/--err` | 代码差异高亮需要高饱和度才能在大段文本里跳出来,并入语义色会变灰、可读性下降 |
| 分类标签 6 色不合并 | 它们的作用就是**互相区分**,合并等于取消功能 |
| `--danger` 不并入 `--err` | `--err` 是橙红(赤陶色系),停止按钮需要纯红的警示强度 |
| `box-shadow` 未做令牌化 | 各处阴影的偏移/模糊/浓度是刻意分层的(浮层高度不同),抽象收益低、误伤风险高 |
| 1~2px 的 padding 微调 | 多为像素级对齐,归并到 4px 阶梯会破坏对齐 |
| 20px 以上大字号/大圆角 | 出现次数少且都是刻意设计的视觉焦点 |

---

## 四、改样式后怎么验

前端改动**不能只靠肉眼和静态分析**,尤其字号变大会触发 `flex-wrap` 换行导致布局意外变高
(2026-08-22 已踩过一次)。

**批量改 CSS 的最低验证线 —— CSSOM 逐规则比对**(能秒查出语法破坏、非法值被丢弃、选择器误伤):

```python
# 用无头浏览器把新旧两版 CSS 分别塞进 <style>,对比每条规则的选择器和声明数
# 数量一致 = 没有任何声明被浏览器判为非法而静默丢弃
JS = "() => { const out=[]; const walk=r=>{for(const x of r){ if(x.style) " \
     "out.push([x.selectorText||'@', x.style.length]); if(x.cssRules) walk(x.cssRules);} }; " \
     "walk(document.getElementById('s').sheet.cssRules); return out; }"
```

**视觉验证**:起本地实例后用 Playwright 在 320/375/390/768/1280 多档屏宽下量
`getBoundingClientRect()`,重点看标题栏/tab 栏有没有被挤成两行。
详细做法见 `~/AI_BRAIN/memory/playwright-ui-e2e.md`。
