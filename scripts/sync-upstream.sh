#!/usr/bin/env bash

set -euo pipefail

readonly expected_branch="autolisten-publishing"
readonly fork_repository="0324wy/social-auto-upload"
readonly upstream_repository="dreammis/social-auto-upload"

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$expected_branch" ]]; then
  echo "错误：请先切换到 $expected_branch，当前分支是 ${current_branch:-detached HEAD}。" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "错误：工作区不干净。请先提交或暂存当前改动，再同步上游。" >&2
  exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "错误：未找到 GitHub CLI（gh）。" >&2
  exit 2
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "错误：未找到 .venv/bin/python，请先按 docs/install.md 安装 Python 环境。" >&2
  exit 2
fi

origin_url="$(git remote get-url origin)"
upstream_url="$(git remote get-url upstream)"
if [[ "$origin_url" != *"0324wy/social-auto-upload"* ]]; then
  echo "错误：origin 不是 $fork_repository：$origin_url" >&2
  exit 2
fi
if [[ "$upstream_url" != *"dreammis/social-auto-upload"* ]]; then
  echo "错误：upstream 不是 $upstream_repository：$upstream_url" >&2
  exit 2
fi

echo "同步 $upstream_repository/main 到 $fork_repository/main ..."
gh repo sync "$fork_repository" \
  --source "$upstream_repository" \
  --branch main

echo "将最新 main 合并到 $expected_branch ..."
git fetch origin main
git merge --no-edit origin/main

echo "运行完整测试 ..."
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

echo "测试通过，推送 $expected_branch ..."
git push origin "$expected_branch"

echo "同步完成。"
