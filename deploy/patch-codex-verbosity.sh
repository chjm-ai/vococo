#!/usr/bin/env bash
# 对 CLIProxyAPI 源码应用兼容补丁；不替换二进制、不重启服务。
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "用法: bash deploy/patch-codex-verbosity.sh /path/to/CLIProxyAPI" >&2
  exit 2
fi
proxy_repo="$1"
patch_file="$(cd "$(dirname "$0")" && pwd)/patches/cliproxyapi-verbosity.patch"
if git -C "$proxy_repo" apply --reverse --check "$patch_file" 2>/dev/null; then
  echo "输出详细程度补丁已应用"
elif git -C "$proxy_repo" apply --check "$patch_file"; then
  git -C "$proxy_repo" apply "$patch_file"
else
  echo "代理源码版本不匹配，未修改文件；请检查补丁上下文。" >&2
  exit 1
fi
(cd "$proxy_repo" && go test ./internal/translator/codex/claude ./internal/translator/openai/claude -count=1)
echo "补丁和测试完成；需按代理原有部署流程构建、替换二进制后生效。"
