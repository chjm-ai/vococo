#!/usr/bin/env zsh
# 把当前 worktree 分支合回 main(worktree 会话专用,在 worktree 里跑)。
# 为什么不能 git checkout main:main 永远被主仓库工作区检出,worktree 里 checkout 必报
# "fatal: 'main' 已经被工作区使用"(exit 128)——已有 7 个会话踩过,一律用本脚本。
# 用法:zsh deploy/merge-main.sh
# 合并后要重启验证:先本脚本完成合并,再调 restart_self 工具(遗书+还魂,重启完
# 自动回当前会话继续验证)。--restart 已移除——restart.sh 是硬杀重启无还魂,
# 会打断发起会话正在生成的回复,不要再传。
set -u

# --restart 已移除(2026-08):restart.sh 硬杀重启无遗书还魂,会打断当前会话回复。
# 合并后要重启验证,两步法:先本脚本合并,再调 restart_self 工具。
if [ "${1:-}" = "--restart" ]; then
  echo "❌ --restart 已移除:硬杀重启(restart.sh)无遗书还魂,会打断当前会话回复。改用两步法:
  1) 先跑本脚本(不带 --restart)完成合并
  2) 再调 restart_self 工具——遗书+还魂,重启完自动回当前会话继续验证" >&2
  exit 1
fi

MAIN="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd)"
BRANCH="$(git branch --show-current)"

[ -z "$BRANCH" ] && { echo "❌ 拿不到当前分支(detached HEAD?)" >&2; exit 1; }
[ "$BRANCH" = "main" ] && { echo "❌ 已在 main,无需合并" >&2; exit 1; }

# 预检 1:本 worktree 全部提交了(未跟踪文件不算)
if [ -n "$(git status --porcelain -uno)" ]; then
  echo "❌ 本 worktree 有未提交改动,先 commit:" >&2
  git status --short -uno >&2
  exit 1
fi
# 未跟踪文件不进合并,提醒但不拦(多半是临时产物;真要带上先 git add + commit)
untracked="$(git ls-files --others --exclude-standard)"
[ -n "$untracked" ] && echo "⚠️ 以下未跟踪文件不会进合并(要带上先 add+commit):\n$untracked"

# 预检 2:主仓库检出的必须是 main(run.sh 每轮重启会自动切回;不在就先别合)
if [ "$(git -C "$MAIN" branch --show-current)" != "main" ]; then
  echo "❌ 主仓库当前不在 main(在 $(git -C "$MAIN" branch --show-current)),先处理再合" >&2
  exit 1
fi
# 预检 3:主仓库工作区干净(脏了合并会失败,也会卡住 run.sh 的 checkout 保险)
if [ -n "$(git -C "$MAIN" status --porcelain -uno)" ]; then
  echo "❌ 主仓库工作区不干净(可能是别的会话留的),先处理:" >&2
  git -C "$MAIN" status --short -uno >&2
  exit 1
fi

echo "合并 $BRANCH → main(main 现在:$(git -C "$MAIN" log --oneline -1 main | cat))"
if git -C "$MAIN" merge --no-ff "$BRANCH" -m "Merge branch '$BRANCH'"; then
  echo "✅ 合并完成:$(git -C "$MAIN" log --oneline -1 main | cat)"
else
  echo "❌ 合并冲突,冲突文件:" >&2
  git -C "$MAIN" diff --name-only --diff-filter=U >&2
  git -C "$MAIN" merge --abort 2>/dev/null
  echo "已 abort,main 保持原样。建议:先在本 worktree 里 git merge main 解决冲突,再重跑本脚本。" >&2
  exit 1
fi
