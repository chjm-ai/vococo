# eval · 编码任务对比评测(Hermes vs Claude Code)

同一编码任务两边各跑一遍,量化谁做得好。为什么这么设计见 `memory/hermes-eval-harness.md`:
纯 QA 考不出差异,编码任务(多步+工具+真改代码)才能考出 harness 差距。

## 用法

```bash
# 1) 改 coding_tasks.json:repo 填绝对路径,prompt/verify 换成真任务(建议用一次性评测 repo)
# 2) 先 dry-run 看流程,不烧额度
python eval/run_coding_bench.py --dry-run
# 3) 真跑(会改目标 repo 的工作区,在受控 git reset 之间;跑 CC 消耗订阅额度)
python eval/run_coding_bench.py
python eval/run_coding_bench.py --only fix-x --tools hermes   # 只跑某任务/某一边
```

## 公平性

- 同模型(`--model`,默认 opus-4-8),两边都走它。
- 每次 run 前 `git reset --hard <baseline>` + `git clean -fd`,同一起点。
- 同 prompt。

## 采集与产物

每 任务 × 每 工具:验收(exit 0=✅)、墙钟秒、改动文件/增删行、报错。
矩阵打印到终端并存 `results/<时间戳>.md`。

## 建议流程

先跑一版**改造前 baseline**(切到改造前的 commit)当锚点,再跑改造后 —— 用数字证明差距缩小了,
而不是凭感觉说「体验变好了」。

⚠️ 只对一次性评测 repo/分支跑,别对正在写的分支跑(会 `git reset --hard` 丢改动)。
