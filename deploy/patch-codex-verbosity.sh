#!/usr/bin/env bash
# 对 CLIProxyAPI 源码应用兼容补丁；不替换二进制、不重启服务。
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "用法: bash deploy/patch-codex-verbosity.sh /path/to/CLIProxyAPI" >&2
  exit 2
fi
proxy_repo="$1"
patch_dir="$(cd "$(dirname "$0")" && pwd)/patches"
command -v go >/dev/null || { echo "请先安装 Go，未修改源码。" >&2; exit 1; }
# 必须是独立 checkout，避免 git 在嵌套目录里跳过全部补丁仍返回成功。
proxy_repo="$(cd "$proxy_repo" && pwd -P)"
repo_root="$(git -C "$proxy_repo" rev-parse --show-toplevel)"
if [ "$repo_root" != "$proxy_repo" ]; then
  echo "请传入 CLIProxyAPI 自身的 Git 仓库根目录，未修改源码。" >&2
  exit 1
fi
patches=("$patch_dir/cliproxyapi-verbosity.patch" "$patch_dir/cliproxyapi-summary.patch")
test_packages=(./internal/thinking ./internal/translator/codex/claude ./internal/translator/openai/claude)
is_v7=false
if grep -q '^module github.com/router-for-me/CLIProxyAPI/v7$' "$proxy_repo/go.mod"; then
  patches=("$patch_dir/cliproxyapi-v7-output-controls.patch")
  is_v7=true
fi
# 所有补丁先预检，防止后一个不兼容时只应用了一半。
for patch_file in "${patches[@]}"; do
  if ! git -C "$proxy_repo" apply --reverse --check "$patch_file" 2>/dev/null; then
    git -C "$proxy_repo" apply --check "$patch_file" || {
      echo "代理源码版本不匹配，未修改文件；请检查补丁上下文。" >&2
      exit 1
    }
  fi
done
for patch_file in "${patches[@]}"; do
  if git -C "$proxy_repo" apply --reverse --check "$patch_file" 2>/dev/null; then
    echo "补丁已应用: $(basename "$patch_file")"
  else
    git -C "$proxy_repo" apply "$patch_file"
  fi
done
go -C "$proxy_repo" test "${test_packages[@]}" -count=1
if "$is_v7"; then
  # v7 的 executor 全量测试有上游既存设备指纹失败，此处只跑新增出站回归。
  go -C "$proxy_repo" test ./internal/runtime/executor -run '^TestVococo' -count=1
fi
echo "补丁和测试完成；需按代理原有部署流程构建、替换二进制后生效。"
