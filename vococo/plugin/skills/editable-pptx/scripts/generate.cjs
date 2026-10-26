#!/usr/bin/env node
"use strict";
// 仅解释声明式内容，不执行用户脚本；正文、表格、图表始终使用原生 PPT 对象。
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {createRequire} = require("node:module");

const WIDTH = 13.333333, HEIGHT = 7.5;
const DEFAULT_THEME = {font: "Microsoft YaHei", background: "F6F4EF", ink: "202C36", accent: "B55336"};
const LAYOUTS = new Set(["cover", "bullets", "columns", "table", "chart", "image"]);

function fail(message) { throw new Error(message); }
function text(value, label, max = 90, multiline = false) {
  if (typeof value !== "string" || !value.trim() || value.length > max || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value) || (!multiline && /[\r\n\t]/.test(value)))
    fail(`${label} 必须是非空文字，最多 ${max} 字符。`);
  return value;
}
function array(value, label, min, max) {
  if (!Array.isArray(value) || value.length < min || value.length > max)
    fail(`${label} 数量须为 ${min}–${max}。`);
  return value;
}
function object(value, label, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} 必须是对象。`);
  for (const key of Object.keys(value)) if (!keys.includes(key)) fail(`${label} 不支持字段 ${key}。`);
}
function items(value, label) {
  array(value, label, 1, 5).forEach((item, i) => text(item, `${label}[${i}]`, 54));
}
function themeFor(raw = {}) {
  object(raw, "theme", Object.keys(DEFAULT_THEME));
  const theme = {...DEFAULT_THEME, ...raw};
  text(theme.font, "theme.font", 80);
  for (const key of ["background", "ink", "accent"])
    if (!/^[0-9a-f]{6}$/i.test(theme[key])) fail(`theme.${key} 必须是六位 RGB。`);
  return theme;
}
function localImage(value, directory) {
  text(value, "image.path", 1024);
  if (path.isAbsolute(value) || value.split(/[\\/]/).some(p => p === ".." || p.startsWith(".")))
    fail("图片须位于内容 JSON 同目录或其子目录，不接受绝对路径/隐藏路径/目录逃逸。");
  let target = directory;
  for (const part of value.split(/[\\/]/)) {
    target = path.join(target, part);
    if (fs.lstatSync(target).isSymbolicLink()) fail("图片不接受软链接。");
  }
  if (!/\.(png|jpe?g)$/i.test(target)) fail("首期图片仅支持本地 PNG/JPEG。");
  const info = fs.statSync(target);
  if (!info.isFile() || info.size > 20 * 1024 * 1024) fail("图片必须是小于 20MB 的普通文件。");
  return target;
}
function validateTable(slide) {
  array(slide.headers, "headers", 2, 5).forEach(v => text(v, "表头", 16));
  array(slide.rows, "rows", 1, 7).forEach(row => {
    array(row, "表格行", slide.headers.length, slide.headers.length).forEach(v => text(v, "单元格", 16));
  });
}
function validateChart(slide) {
  if (!["bar", "line", "pie"].includes(slide.chartType)) fail("chartType 只支持 bar / line / pie。");
  array(slide.labels, "labels", 2, 8).forEach(v => text(v, "图表标签", 12));
  array(slide.series, "series", 1, slide.chartType === "pie" ? 1 : 4).forEach(series => {
    object(series, "series", ["name", "values"]);
    text(series.name, "系列名", 18);
    array(series.values, "values", slide.labels.length, slide.labels.length).forEach(v => {
      if (typeof v !== "number" || !Number.isFinite(v) || Math.abs(v) > 1e12) fail("图表数值须为有限数值，绝对值不超过 1e12。");
      if (slide.chartType === "pie" && v < 0) fail("饼图不接受负数。");
    });
    if (slide.chartType === "pie" && !series.values.some(v => v > 0)) fail("饼图须有大于零的数值。");
  });
}
function validateSlide(slide, directory) {
  const fields = {
    cover: ["subtitle"], bullets: ["items"], columns: ["columns"],
    table: ["headers", "rows"], chart: ["chartType", "labels", "series"], image: ["path", "caption"],
  };
  if (!slide || !LAYOUTS.has(slide.layout)) fail("layout 须为 cover/bullets/columns/table/chart/image。");
  object(slide, "slide", ["layout", "title", "notes", ...fields[slide.layout]]);
  text(slide.title, "幻灯片标题", 32);
  if (slide.notes !== undefined) text(slide.notes, "备注", 4000, true);
  if (slide.layout === "cover" && slide.subtitle !== undefined) text(slide.subtitle, "副标题", 72);
  if (slide.layout === "bullets") items(slide.items, "items");
  if (slide.layout === "columns") array(slide.columns, "columns", 2, 2).forEach(column => {
    object(column, "column", ["title", "items"]);
    text(column.title, "分栏标题", 18); items(column.items, "分栏条目");
    column.items.forEach(value => text(value, "分栏条目", 28));
  });
  if (slide.layout === "table") validateTable(slide);
  if (slide.layout === "chart") validateChart(slide);
  if (slide.layout === "image") {
    localImage(slide.path, directory);
    if (slide.caption !== undefined) text(slide.caption, "图片说明", 72);
  }
}
function validate(data, directory) {
  object(data, "演示稿", ["title", "author", "theme", "slides"]);
  text(data.title, "title", 120);
  if (data.author !== undefined) text(data.author, "author", 80);
  themeFor(data.theme);
  array(data.slides, "slides", 1, 50).forEach(slide => validateSlide(slide, directory));
}
function library() {
  const cache = process.env.VOCOCO_PPTX_HOME || path.join(os.homedir(), ".cache/vococo-pptx/4.0.1");
  let PptxGenJS;
  try { PptxGenJS = createRequire(path.join(cache, "package.json"))("pptxgenjs"); }
  catch { fail("缺少 PPT 依赖；请先由维护者运行此技能 scripts/setup.py，生成过程不会自动装包。"); }
  return PptxGenJS;
}
function box(slide, theme, value, x, y, w, h, size = 22, extra = {}) {
  if (x < 0 || y < 0 || x + w > WIDTH + 0.01 || y + h > HEIGHT + 0.01) fail("内部版式越界。");
  slide.addText(value, {x, y, w, h, fontFace: theme.font, fontSize: size, color: theme.ink,
    margin: 0, breakLine: false, valign: "mid", ...extra});
}
function base(pptx, theme, title, index) {
  const slide = pptx.addSlide();
  slide.background = {color: theme.background};
  slide.addShape(pptx.ShapeType.rect, {x: .6, y: .48, w: .12, h: .42, fill: {color: theme.accent}, line: {transparency: 100}});
  box(slide, theme, title, .92, .42, 11.7, .7, 26, {bold: true});
  box(slide, theme, String(index + 1).padStart(2, "0"), 12, 7, .65, .22, 10, {align: "right"});
  return slide;
}
function bulletList(slide, theme, entries, x, y, w, size = 23) {
  entries.forEach((entry, i) => {
    box(slide, theme, String(i + 1).padStart(2, "0"), x, y + i * .91, .48, .58, 15, {color: theme.accent, bold: true});
    box(slide, theme, entry, x + .64, y + i * .91, w - .64, .75, size);
  });
}
function cover(slide, theme, content) {
  box(slide, theme, content.title, .95, 2.05, 11.3, 1.55, 40, {bold: true});
  if (content.subtitle) box(slide, theme, content.subtitle, 1, 4.1, 10.8, 1.1, 23);
}
function columns(slide, theme, content) {
  content.columns.forEach((column, i) => {
    const x = .95 + i * 6.1;
    box(slide, theme, column.title, x, 1.55, 5.4, .55, 25, {color: theme.accent, bold: true});
    bulletList(slide, theme, column.items, x, 2.35, 5.3, 19);
  });
}
function table(slide, theme, content) {
  const head = content.headers.map(value => ({text: value, options: {bold: true, color: "FFFFFF", fill: theme.accent}}));
  slide.addTable([head, ...content.rows], {x: .95, y: 1.65, w: 11.4, colW: Array(content.headers.length).fill(11.4 / content.headers.length),
    rowH: .65, fontFace: theme.font, fontSize: 15, color: theme.ink, fill: "FFFFFF", margin: .1,
    border: {type: "solid", color: theme.background, pt: 1}, valign: "mid", autoPage: false});
}
function chart(pptx, slide, theme, content) {
  const data = content.series.map(series => ({name: series.name, labels: content.labels, values: series.values}));
  slide.addChart(pptx.ChartType[content.chartType], data, {x: 1, y: 1.6, w: 11.25, h: 4.95,
    catAxisLabelFontFace: theme.font, catAxisLabelFontSize: 14, valAxisLabelFontFace: theme.font,
    valAxisLabelFontSize: 12, legendFontFace: theme.font, legendFontSize: 13,
    chartColors: [theme.accent, "426C82", "68835D", "A17C41"], showLegend: content.series.length > 1 || content.chartType === "pie",
    showValue: true, showTitle: false, showBorder: false, showCatName: content.chartType === "pie",
    showPercent: content.chartType === "pie", showMarker: true, holeSize: 0,
    showShadow: false, catAxisLabelRotate: 0, layout: {x: .12, y: .08, w: .8, h: .75}});
}
function image(pptx, slide, theme, content, directory) {
  const file = localImage(content.path, directory);
  slide.addImage({path: file, x: 1, y: 1.6, w: 11.25, h: 4.55,
    sizing: {type: "contain", x: 1, y: 1.6, w: 11.25, h: 4.55}});
  if (content.caption) box(slide, theme, content.caption, 1, 6.35, 11.25, .48, 16);
}
async function generate(data, output, directory) {
  validate(data, directory);
  const PptxGenJS = library(), pptx = new PptxGenJS(), theme = themeFor(data.theme);
  pptx.layout = "LAYOUT_WIDE"; pptx.title = data.title; pptx.author = data.author || "vococo";
  pptx.subject = "可编辑演示稿"; pptx.lang = "zh-CN";
  pptx.theme = {headFontFace: theme.font, bodyFontFace: theme.font, lang: "zh-CN"};
  data.slides.forEach((content, i) => {
    const slide = base(pptx, theme, content.layout === "cover" ? "" : content.title, i);
    if (content.layout === "cover") cover(slide, theme, content);
    if (content.layout === "bullets") bulletList(slide, theme, content.items, 1, 1.65, 11.2);
    if (content.layout === "columns") columns(slide, theme, content);
    if (content.layout === "table") table(slide, theme, content);
    if (content.layout === "chart") chart(pptx, slide, theme, content);
    if (content.layout === "image") image(pptx, slide, theme, content, directory);
    if (content.notes) slide.addNotes(content.notes);
  });
  // wx 防止静默覆盖；失败的生成不留下半成品文件。
  const bytes = await pptx.write({outputType: "nodebuffer"});
  fs.writeFileSync(output, bytes, {flag: "wx"});
  return {output, slides: data.slides.length, bytes: bytes.length, editable: ["text", "shapes", "tables", "charts"],
    visual_check: "pending: 请运行 check.py；未渲染不能宣称排版验收通过"};
}
async function main(args) {
  if (args.length !== 2) fail("用法：node generate.cjs 内容.json 新文件.pptx（不覆盖已有文件）");
  const input = path.resolve(args[0]), output = path.resolve(args[1]);
  if (!/\.pptx$/i.test(output)) fail("输出必须使用 .pptx 后缀。");
  if (fs.existsSync(output)) fail("输出文件已存在，请使用新文件名。");
  if (fs.statSync(input).size > 1024 * 1024) fail("内容 JSON 不得超过 1MB。");
  const data = JSON.parse(fs.readFileSync(input, "utf8"));
  console.log(JSON.stringify(await generate(data, output, path.dirname(input)), null, 2));
}
if (require.main === module) main(process.argv.slice(2)).catch(error => {console.error(`PPT 生成失败：${error.message}`); process.exitCode = 1;});
// AI 可直接取得完整原生 API；固定 JSON 生成器仅保留旧稿兼容。
module.exports = {validate, generate, library};
