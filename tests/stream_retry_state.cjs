// 上游重试状态机(前端):把 stream.js【真身】里的 retryLabel 与 upstream_retry 分支
// 抠出来原样求值,不起服务、不依赖 DOM。跑法:node tests/stream_retry_state.cjs
//
// 为什么要有这层:pytest 只覆盖到 SSE 那一帧(见 tests/test_upstream_retry.py),
// 真正决定"屏幕上显示什么"的是 stream.js 里这段状态机——重试状态什么时候显示、
// 什么时候被清掉、重放乱序时会不会把过期状态又贴回去。2026-10-15 加了上游重试可见性
// 之后,评审就是在这块空档里发现"切回会话时重放会把过期重试帧排到最后"的。
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

const src = fs.readFileSync(
  path.join(__dirname, "../vococo/gateway/adapters/web_static/stream.js"), "utf8"
);

// 从源码里按名字抠出一个函数(括号配对),返回它的源码文本
function grab(name) {
  const i = src.indexOf("function " + name + "(");
  assert.ok(i > 0, "stream.js 里找不到函数 " + name);
  let depth = 0;
  const start = src.indexOf("{", i);
  for (let k = start; k < src.length; k++) {
    if (src[k] === "{") depth++;
    else if (src[k] === "}" && --depth === 0) return src.slice(i, k + 1);
  }
  throw new Error("括号不配对: " + name);
}

const retryLabel = eval("(" + grab("retryLabel") + ")");
const noteContent = eval("(" + grab("noteContent") + ")");
const clearUpstreamRetry = eval("(" + grab("clearUpstreamRetry") + ")");

// upstream_retry 的 case 块(带守卫),原样塞进一个 switch 里执行
const caseStart = src.indexOf('case "upstream_retry": {');
assert.ok(caseStart > 0, "stream.js 里找不到 upstream_retry 分支");
let depth = 0, caseEnd = -1;
for (let k = src.indexOf("{", caseStart); k < src.length; k++) {
  if (src[k] === "{") depth++;
  else if (src[k] === "}" && --depth === 0) { caseEnd = k; break; }
}
const caseBlock = src.slice(caseStart, caseEnd + 1);
const runCase = new Function("e", "s", "ensureStream", "renderStatus",
  "switch(e.type){" + caseBlock + "}");
// CONTENT_EVENTS 是文件顶层的 const,函数体里引用不到,这里按同一份名单复刻(改名单要同步)
const CONTENT_EVENTS = new Set(["thinking", "text", "tool_start", "tool_input", "tool_end", "message", "compact"]);
assert.ok(/const CONTENT_EVENTS = new Set\(\[([^\]]*)\]\)/.test(src), "CONTENT_EVENTS 定义变了,请同步本测试");

// 重放一串帧,返回末态(复刻 applyStreamEvent 的入口顺序:内容帧先记账再走 case)
function replay(frames, state) {
  const s = Object.assign({ retry: null, lastContentSeq: 0 }, state || {});
  for (const e of frames) {
    if (e.id && CONTENT_EVENTS.has(e.type)) noteContent(s, e);
    runCase(e, s, () => s, () => {});
  }
  return s;
}

const retry = (id, attempt) => ({
  type: "upstream_retry", id, status: 503, attempt,
  max_retries: 10, delay_ms: 18000,
});

// ── 文案 ────────────────────────────────────────────────────────────────
{
  const t = 1_000_000, realNow = Date.now;
  Date.now = () => t;
  assert.equal(retryLabel({ status: 503, attempt: 3, max: 10, delay: 18000, at: t }),
    "上游 503 · 重试 3/10 · 18s 后重试");
  Date.now = () => t + 8000;
  assert.equal(retryLabel({ status: 503, attempt: 3, max: 10, delay: 18000, at: t }),
    "上游 503 · 重试 3/10 · 10s 后重试", "退避倒计时要跟着走字");
  Date.now = () => t + 20000;
  assert.equal(retryLabel({ status: 503, attempt: 3, max: 10, delay: 18000, at: t }),
    "上游 503 · 重试 3/10 · 请求中", "退避到点应显示已重新发出");
  Date.now = () => t;
  assert.equal(retryLabel({ status: 0, attempt: 0, max: 0, delay: 0, at: t }), "重试中",
    "字段全缺(CLI 换字段名)也必须有一句人话,不能空");
  assert.equal(retryLabel({ status: 429, attempt: 1, max: 10, delay: 600, at: t }),
    "上游 429 · 重试 1/10 · 1s 后重试");
  assert.equal(retryLabel({ status: 0, attempt: 2, max: 0, delay: 0, at: t }), "重试 2");
  Date.now = realNow;
}

// ── 状态机 ──────────────────────────────────────────────────────────────
// ① 直播:内容之后来一条重试 → 显示
assert.equal(replay([{ type: "thinking", id: 10 }, retry(12, 1)]).retry.attempt, 1);
// ② 重放乱序:更新的内容帧(原位替换后)排在重试帧后面 → 过期帧,不许贴回状态行
assert.equal(replay([{ type: "thinking", id: 18 }, retry(5, 1)]).retry, null,
  "过期重试帧会把状态行改回「重试中」");
// ③ 同一轮里的第二次重试:旧帧拒、新帧显示
assert.equal(replay([retry(5, 1), { type: "text", id: 9 }, retry(15, 2)]).retry.attempt, 2);
// ④ 缓冲里全是过期帧 → 不显示
assert.equal(replay([{ type: "thinking", id: 18 }, retry(5, 1), retry(15, 2)]).retry, null);
// ⑤ 没有 id 的老帧不误伤
assert.equal(replay([retry(undefined, 7)]).retry.attempt, 7);
// ⑥ 首帧就是重试(还没有任何内容,水位为 0)→ 显示
assert.equal(replay([retry(3, 1)]).retry.attempt, 1);
// ⑦ 重试之后模型出正文 → 清掉
assert.equal(replay([retry(20, 1), { type: "text", id: 21 }]).retry, null);
// ⑧ 工具事件同样算"有进展"
assert.equal(replay([retry(20, 1), { type: "tool_start", id: 22 }]).retry, null);
// ⑨ restore-from-snapshot:离开期间发生的原位替换不进增量,但水位要按整段缓冲垫高
//    (stream.js maybeReplayStream 里的 lastContentSeq 预扫),垫高后 13 < 14 被拒
assert.equal(replay([retry(13, 1)], { lastContentSeq: 14 }).retry, null);
// ⑩ 垫高水位不能把"比内容更新"的重试帧一起拒掉
assert.equal(replay([retry(15, 9)], { lastContentSeq: 14 }).retry.attempt, 9);

console.log("stream_retry_state: 文案 6 例 + 状态机 10 例全部通过");
