#!/usr/bin/env zsh
# 把当前 worktree 分支合回 main(worktree 会话专用,在 worktree 里跑)。
# 为什么不能 git checkout main:main 永远被主仓库工作区检出,worktree 里 checkout 必报
# "fatal: 'main' 已经被工作区使用"(exit 128)——已有 7 个会话踩过,一律用本脚本。
# 用法:zsh deploy/merge-main.sh [--restart]
#   --restart:合并成功后接着跑 deploy/restart.sh 让 serve 加载新代码。
set -u

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

# ── 收尾清理:内容已进 main,worktree 和分支没有存在意义了,当场回收 ──
# 预检已保证 tracked 无未提交改动;未跟踪文件(临时产物)先归档再删,不留无声丢失。
# 跑本脚本 = 会话收尾的约定:清完 worktree,会话下轮对话会自动重建(见 worktree.py
# ensure_worktree 的惰性创建),历史改动全在 main 里,无任何损失。
WT="$(pwd)"
case "$WT" in
  */data/worktrees/*)
    untracked="$(git ls-files --others --exclude-standard)"
    if [ -n "$untracked" ]; then
      archive_dir="$MAIN/data/worktree-archive"
      mkdir -p "$archive_dir"
      # 分支名含 / (hermes/xxx),归档文件名换成 _ 避免目录缺失
      fname="$(echo "$BRANCH" | tr '/' '_').tgz"
      if echo "$untracked" | tar -czf "$archive_dir/$fname" -T - 2>/dev/null; then
        echo "📦 未跟踪文件已归档: $archive_dir/$fname"
      else
        echo "⚠️ 未跟踪文件归档失败,将随 worktree 一起删除:" >&2
        echo "$untracked" >&2
      fi
    fi
    cd "$MAIN"  # 先离开再删自己——进程 cwd 占着目录删不掉
    if git worktree remove --force "$WT" && git branch -d "$BRANCH" && git worktree prune; then
      echo "🧹 已回收会话 worktree+分支: $BRANCH"
    else
      echo "⚠️ worktree 回收失败(内容已进 main 不受影响,可手动清:git worktree remove $WT)" >&2
    fi
    ;;
esac

if [ "${1:-}" = "--restart" ]; then
  # worktree 路径是 data/worktrees/<项目哈希>/<会话slug>,对应 web 会话键
  # web:p<哈希>:<slug>(见 gateway/adapters/web.py:479)——推给 restart.sh 自我排除,
  # 这样"我自己"不会被当成"别的在跑会话"拦下来,只有真有别的会话在跑才会提示确认。
  # WT 在上面已取好(此刻 cwd 已是主仓库),此处只解析,不再 pwd。
  SELF_KEY=""
  case "$WT" in
    */data/worktrees/*/*)
      SLUG="$(basename "$WT")"
      HASH="$(basename "$(dirname "$WT")")"
      SELF_KEY="web:p${HASH}:${SLUG}"
      ;;
  esac
  if [ -n "$SELF_KEY" ]; then
    exec zsh "$MAIN/deploy/restart.sh" "--self=$SELF_KEY"
  else
    exec zsh "$MAIN/deploy/restart.sh"
  fi
fi
