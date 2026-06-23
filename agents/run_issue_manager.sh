#!/usr/bin/env bash
# GitHub Issue Manager — 실행 래퍼
# 사용법: ./run_issue_manager.sh [--dry-run] [repo_path] [owner/repo]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 인자 파싱 ────────────────────────────────────────────────────────────────
DRY_RUN="false"
REPO_PATH="${REPO_PATH:-/data/workspace/knowledgebuilder}"
GITHUB_REPO="${GITHUB_REPO:-}"

for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN="true" ;;
    --*)          echo "알 수 없는 옵션: $arg"; exit 1 ;;
    */*)          GITHUB_REPO="$arg" ;;    # owner/repo 형식
    /*)           REPO_PATH="$arg" ;;     # 절대경로
  esac
done

# ── 리포 없으면 클론 ─────────────────────────────────────────────────────────
if [[ ! -d "$REPO_PATH" ]]; then
  if [[ -z "$GITHUB_REPO" ]]; then
    echo "❌  REPO_PATH($REPO_PATH)가 없고 GITHUB_REPO도 지정되지 않았습니다."
    echo "    사용법: GITHUB_REPO=owner/repo ./run_issue_manager.sh"
    exit 1
  fi
  echo "📥  $GITHUB_REPO 클론 → $REPO_PATH"
  mkdir -p "$(dirname "$REPO_PATH")"
  git clone "https://github.com/${GITHUB_REPO}.git" "$REPO_PATH"
fi

# ── 환경변수 내보내기 ─────────────────────────────────────────────────────────
export REPO_PATH
export GITHUB_REPO
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export DRY_RUN
export STALE_DAYS="${STALE_DAYS:-30}"
export DUPLICATE_THRESHOLD="${DUPLICATE_THRESHOLD:-0.75}"

echo "================================================"
echo "  GitHub Issue Manager"
echo "  REPO_PATH: $REPO_PATH"
echo "  GITHUB_REPO: $GITHUB_REPO"
echo "  DRY_RUN: $DRY_RUN"
echo "================================================"

python3 "$SCRIPT_DIR/github_issue_manager.py"
