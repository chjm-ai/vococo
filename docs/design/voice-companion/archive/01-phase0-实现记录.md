# P0 实现记录

## 新增文件

```
vococo/voice/
  __init__.py        # register_routes(app) 唯一对外入口
  routes.py           # aiohttp 路由:页面 + stt + 对话 SSE + stop
  session.py          # 语音会话历史(data/voice/voice.db)+ 调 stream_turn
  stt.py              # SenseVoice 转写(复刻 web.py 的 /transcribe,零耦合)
  tts.py              # 句子聚合器 + edge-tts 合成
  prompts.py          # 语音模式指令块模板 + 【屏幕】标记切分
  static/voice.html   # 独立页面(单文件 HTML+CSS+JS)
tests/test_voice_p0.py
```

## 接触点实际改动(对照 00-overview.md §2.2 预算)

| 文件 | 预算 | 实际 | 内容 |
|---|---|---|---|
| `config.py` | ≤10 行 | 8 行 | `VOICE_ENABLED`、`VOICE_TTS_VOICE` |
| `web.py` | ≤5 行 | 4 行 | `_start_server()` 里 `if config.VOICE_ENABLED: register_routes(app)` |
| `index.html` | ≤15 行 | 5 行 | 顶部栏 🎙️ 按钮 + `/voice/config` 探测显隐 |
| `agent.py` | 0(P0 不允许) | 0 | 未碰 |
| `pyproject.toml` | 文档明确允许加依赖 | 2 行 | `edge-tts>=6.1.0` 依赖 + voice 包 package-data |
| 其余现有文件 | 0 | 0 | 未碰 |

全部在预算内。`git diff --stat` 复核见下方「移除清单复核」。

## 技术决策 / 与设计文档的差异

- **鉴权**:00-overview.md §4 说 voice 路由"自动被 WEB_AUTH_TOKEN 保护",实测
  `_security_mw` 只管跨源拦截和安全头,真正校验口令的是各 handler 自己调
  `_ok_token`/`_guard`(逐 handler 显式调用,不是中间件兜底)。voice/routes.py
  复刻了同一形态(`_ok_token`/`_guard`),`/voice`、`/voice/config` 不校验
  (前者是页面壳、后者只暴露一个布尔开关,均非敏感),其余三个 POST 路由校验。
  voice.html 直接读 `localStorage.getItem("vococo_token")`(与主界面同源共享),
  不需要再登录一次。
- **SSE 传输**:音频走 base64 内嵌在 `event: sentence` 里,没有单独实现
  `GET /voice/tts?sid=`——doc §4.2 允许实现者二选一,内嵌更简单且状态更少。
- **停止语义**:F8 只清空音频合成/播放,不打断 `stream_turn` 本身的文字生成
  (打断 SDK 流会牵扯保温池/resume 状态,且 P0 明确不做"打断检测",只做"停止
  按钮"——保留完整文字流入历史更安全,只是不再出声)。
- **工具集**:voice 会话复用 `stream_turn` 默认加载的全量 vococo 工具/skills
  (与主 web 会话一致),P0 未额外收紧。风险:首轮系统提示较大,但走 prompt
  cache,和现有主会话的延迟特征一致,不算本期新增风险。

## 手工验收(自查清单)

- [x] `uv run pytest` 全绿(177 通过,含 11 个新增语音测试)。
- [x] `register_routes(app)` 挂载出 5 条路由(`/voice`、`/voice/config`、
      `/voice/stt`、`/voice/send`、`/voice/stop`),`web.py` 正常 import。
- [ ] 手机 Safari 实机走一遍 §2 用户故事(按住说话→转写回显→AI 语音+文字
      回复→停止按钮→`VOICE_ENABLED=0` 回归)——**待用户在真机上验证**,
      尤其 iOS 音频解锁(voice.html 里 `unlockAudio()`)是否在真实 Safari
      上对"松手后异步到达的音频"依然生效,这是设计文档明确点名的高风险点。

## 移除清单复核(00-overview.md §2.4)

已做「移除演练」(见下方命令,`git stash` 式验证,未真删):

```bash
git stash push -u -- vococo/voice tests/test_voice_p0.py \
  vococo/gateway/adapters/web.py vococo/gateway/adapters/web_static/index.html \
  vococo/config.py pyproject.toml uv.lock
uv run pytest -q   # 166 passed —— 回到语音功能之前的基线,无残留依赖
git stash pop      # 验证完毕,原样恢复
```

真要彻底移除时步骤不变:

1. `rm -rf vococo/voice/ tests/test_voice_p0.py docs/design/voice-companion/`
2. 还原 `web.py` 那 4 行、`index.html` 那 5 行、`config.py` 那 8 行
3. `rm -rf data/voice/`(本期尚未在真实环境跑过,大概率还不存在)
4. `.env` 删掉 `VOICE_` 前缀变量(`VOICE_ENABLED`、`VOICE_TTS_VOICE`)
5. `pyproject.toml` 去掉 `edge-tts` 依赖行 + voice 包的 package-data 条目,
   `uv sync` 重新锁定 `uv.lock`

## 下一步

P1(任务板:派活/查进度/汇报)需要给 `core/agent.py` 的 `stream_turn` 加一个
可选 `extra_mcp_servers` 参数(≤10 行,P0 未触碰)。P0 完成、待合并验证后再启动。
