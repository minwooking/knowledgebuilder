#!/usr/bin/env python3
"""
GitHub Issue Manager Agent
- git fetch & pull origin master
- 중복 이슈 감지 및 그룹화
- 종결된 이슈 자동 close
- 담당자(assignee) 및 이슈 작성자에게 요약 공유
"""

import os
import sys
import json
import subprocess
import re
import logging
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ── 설정 ─────────────────────────────────────────────────────────────────────
REPO_PATH = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")          # e.g. "owner/repo"
DUPLICATE_THRESHOLD = float(os.environ.get("DUPLICATE_THRESHOLD", "0.75"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── 로거 설정 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Git 작업
# ─────────────────────────────────────────────────────────────────────────────

def run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    """git 명령 실행 후 (성공여부, 출력) 반환."""
    cmd = ["git"] + args
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    output = result.stdout.strip() + result.stderr.strip()
    return result.returncode == 0, output


def git_sync(repo_path: str) -> dict:
    """fetch + pull origin master 실행."""
    report = {"fetch": None, "pull": None, "errors": []}

    if not os.path.isdir(repo_path):
        msg = f"디렉토리 없음: {repo_path}"
        log.error(msg)
        report["errors"].append(msg)
        return report

    log.info("git fetch ...")
    ok, out = run_git(["fetch", "--prune", "origin"], repo_path)
    report["fetch"] = out
    if not ok:
        report["errors"].append(f"fetch 실패: {out}")
        log.warning("fetch 실패: %s", out)

    log.info("git pull origin master ...")
    ok, out = run_git(["pull", "origin", "master"], repo_path)
    report["pull"] = out
    if not ok:
        # main 브랜치도 시도
        log.info("master 실패, main 브랜치 시도...")
        ok, out = run_git(["pull", "origin", "main"], repo_path)
        report["pull"] = out
        if not ok:
            report["errors"].append(f"pull 실패: {out}")
            log.warning("pull 실패: %s", out)

    log.info("git sync 완료")
    return report


# ─────────────────────────────────────────────────────────────────────────────
# 2. GitHub API 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

class GitHubAPI:
    BASE = "https://api.github.com"

    def __init__(self, token: str, repo: str):
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, path: str, params: dict = None) -> list | dict:
        url = f"{self.BASE}/repos/{self.repo}{path}"
        results = []
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                return data
            url = resp.links.get("next", {}).get("url")
            params = None  # next URL에는 이미 params 포함됨
        return results

    def _post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{self.BASE}/repos/{self.repo}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> dict:
        resp = self.session.patch(f"{self.BASE}/repos/{self.repo}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def list_open_issues(self) -> list[dict]:
        """PR 제외 열린 이슈 전체 반환."""
        issues = self._get("/issues", {"state": "open", "per_page": 100})
        return [i for i in issues if "pull_request" not in i]

    def list_closed_issues(self, days: int = 14) -> list[dict]:
        """최근 N일 이내 닫힌 이슈 반환."""
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        since = since - timedelta(days=days)
        issues = self._get("/issues", {
            "state": "closed",
            "per_page": 100,
            "since": since.isoformat().replace("+00:00", "Z"),
        })
        return [i for i in issues if "pull_request" not in i]

    def close_issue(self, number: int, comment: str = "") -> None:
        if comment and not DRY_RUN:
            self._post(f"/issues/{number}/comments", {"body": comment})
        if not DRY_RUN:
            self._patch(f"/issues/{number}", {"state": "closed"})
        log.info("[%s] #%d close%s", "DRY" if DRY_RUN else "OK", number,
                 " (dry)" if DRY_RUN else "")

    def add_label(self, number: int, labels: list[str]) -> None:
        if not DRY_RUN:
            self._post(f"/issues/{number}/labels", {"labels": labels})

    def post_comment(self, number: int, body: str) -> None:
        if not DRY_RUN:
            self._post(f"/issues/{number}/comments", {"body": body})
        log.info("[%s] #%d 댓글 게시%s", "DRY" if DRY_RUN else "OK", number,
                 " (dry)" if DRY_RUN else "")

    def get_repo_info(self) -> dict:
        resp = self.session.get(f"{self.BASE}/repos/{self.repo}")
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 중복 이슈 감지
# ─────────────────────────────────────────────────────────────────────────────

def detect_duplicates(issues: list[dict], threshold: float = 0.75) -> list[list[dict]]:
    """
    TF-IDF 코사인 유사도로 중복 이슈 그룹화.
    반환: [[이슈A, 이슈B, ...], ...] (그룹 크기 ≥ 2인 것만)
    """
    if len(issues) < 2:
        return []

    texts = [
        f"{i['title']} {i.get('body') or ''}".strip()
        for i in issues
    ]

    vect = TfidfVectorizer(
        min_df=1,
        ngram_range=(1, 2),
        strip_accents="unicode",
        sublinear_tf=True,
    )
    try:
        tfidf = vect.fit_transform(texts)
    except ValueError:
        return []

    sim = cosine_similarity(tfidf)

    visited = set()
    groups = []
    for i in range(len(issues)):
        if i in visited:
            continue
        group = [i]
        for j in range(i + 1, len(issues)):
            if j not in visited and sim[i, j] >= threshold:
                group.append(j)
                visited.add(j)
        if len(group) > 1:
            groups.append([issues[k] for k in group])
        visited.add(i)

    return groups


# ─────────────────────────────────────────────────────────────────────────────
# 4. 종결 이슈 감지 (자동 close 후보)
# ─────────────────────────────────────────────────────────────────────────────

RESOLVED_KEYWORDS = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|완료|해결|닫|종결)\b",
    re.IGNORECASE,
)
STALE_DAYS = int(os.environ.get("STALE_DAYS", "30"))


def is_likely_resolved(issue: dict) -> str | None:
    """
    종결로 볼 수 있는 이유를 문자열로 반환; 해당없으면 None.
    """
    # 라벨에 resolved / won't fix / duplicate 포함
    labels = [l["name"].lower() for l in issue.get("labels", [])]
    if any(kw in lbl for kw in ("resolved", "wontfix", "won't fix", "duplicate", "invalid")
           for lbl in labels):
        return f"라벨: {labels}"

    body = (issue.get("body") or "").lower()
    title = issue.get("title", "").lower()

    # 제목/본문에 완료 키워드
    if RESOLVED_KEYWORDS.search(title) or RESOLVED_KEYWORDS.search(body):
        return "제목/본문에 완료 키워드 포함"

    # 업데이트 없이 장기 미활동
    updated_at = issue.get("updated_at", "")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - updated
            if delta.days >= STALE_DAYS:
                return f"{delta.days}일 미활동 (stale)"
        except ValueError:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. 담당자 / 작성자별 알림 메시지 생성
# ─────────────────────────────────────────────────────────────────────────────

def build_user_summaries(
    duplicate_groups: list[list[dict]],
    auto_closed: list[tuple[dict, str]],
    repo: str,
) -> dict[str, str]:
    """
    GitHub 사용자 login → 마크다운 요약 메시지.
    """
    user_map: dict[str, list[str]] = defaultdict(list)

    # 중복 이슈
    for group in duplicate_groups:
        keeper = group[0]
        dups = group[1:]
        for issue in group:
            login_set = {issue["user"]["login"]}
            for a in issue.get("assignees") or []:
                login_set.add(a["login"])
            msg = (
                f"**[중복 이슈 그룹]** #{keeper['number']} 을 대표 이슈로 유지합니다.\n"
                f"- 대표: #{keeper['number']} {keeper['title']}\n"
                + "\n".join(f"- 중복: #{d['number']} {d['title']}" for d in dups)
            )
            for login in login_set:
                user_map[login].append(msg)

    # 자동 종결 이슈
    for issue, reason in auto_closed:
        login_set = {issue["user"]["login"]}
        for a in issue.get("assignees") or []:
            login_set.add(a["login"])
        msg = (
            f"**[자동 종결]** #{issue['number']} {issue['title']}\n"
            f"- 사유: {reason}\n"
            f"- 링크: {issue['html_url']}"
        )
        for login in login_set:
            user_map[login].append(msg)

    # 최종 마크다운 조합
    summaries = {}
    for login, items in user_map.items():
        header = (
            f"안녕하세요 @{login} 님,\n\n"
            f"**{repo}** 리포지토리 이슈 정리 결과를 공유드립니다. "
            f"({datetime.now().strftime('%Y-%m-%d')})\n\n"
        )
        body = "\n\n---\n\n".join(items)
        footer = "\n\n> 이 메시지는 자동 이슈 관리 에이전트가 생성했습니다."
        summaries[login] = header + body + footer

    return summaries


# ─────────────────────────────────────────────────────────────────────────────
# 6. 이슈에 댓글로 알림 게시
# ─────────────────────────────────────────────────────────────────────────────

def notify_on_issues(
    api: GitHubAPI,
    duplicate_groups: list[list[dict]],
    auto_closed: list[tuple[dict, str]],
) -> None:
    """각 이슈에 직접 댓글 게시."""

    # 중복 그룹 처리
    for group in duplicate_groups:
        keeper = group[0]
        dups = group[1:]
        dup_refs = " ".join(f"#{d['number']}" for d in dups)
        assignees = list({
            a["login"]
            for issue in group
            for a in (issue.get("assignees") or [])
        })
        mentions = " ".join(f"@{a}" for a in assignees) if assignees else ""

        # 대표 이슈에 중복 안내 댓글
        keeper_comment = (
            f"🔗 **중복 이슈 묶음** {mentions}\n\n"
            f"다음 이슈가 이 이슈({keeper['title']})와 유사한 내용으로 감지되었습니다:\n"
            + "\n".join(f"- #{d['number']} {d['title']}" for d in dups)
            + "\n\n중복 이슈는 이 이슈에 통합하여 진행 권장합니다."
        )
        api.post_comment(keeper["number"], keeper_comment)

        # 중복 이슈에 닫힘 안내
        for dup in dups:
            dup_comment = (
                f"🚫 **중복 이슈 종결** @{dup['user']['login']}\n\n"
                f"#{keeper['number']} ({keeper['title']}) 와 유사한 내용으로 "
                f"자동 종결됩니다. 해당 이슈에서 계속 논의해 주세요.\n"
                f"- 대표 이슈: {keeper['html_url']}"
            )
            api.close_issue(dup["number"], dup_comment)
            try:
                api.add_label(dup["number"], ["duplicate"])
            except Exception:
                pass

    # 자동 종결 이슈 처리
    for issue, reason in auto_closed:
        assignees = [a["login"] for a in (issue.get("assignees") or [])]
        mentions = " ".join(f"@{a}" for a in assignees) if assignees else ""
        author_mention = f"@{issue['user']['login']}"

        comment = (
            f"✅ **자동 종결** {author_mention} {mentions}\n\n"
            f"- 종결 사유: {reason}\n\n"
            f"이슈가 해결된 것으로 판단되어 자동으로 종결합니다. "
            f"재논의가 필요하면 이슈를 다시 열어주세요."
        )
        api.close_issue(issue["number"], comment)


# ─────────────────────────────────────────────────────────────────────────────
# 7. 요약 이슈(리포트) 생성
# ─────────────────────────────────────────────────────────────────────────────

def post_summary_issue(
    api: GitHubAPI,
    git_report: dict,
    duplicate_groups: list[list[dict]],
    auto_closed: list[tuple[dict, str]],
    repo: str,
) -> None:
    """전체 작업 결과를 새 이슈로 게시."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 📋 이슈 관리 자동 보고서 ({now})\n"]

    # git 상태
    lines.append("## Git 동기화")
    if git_report.get("errors"):
        lines.append("⚠️ 오류 발생:")
        for e in git_report["errors"]:
            lines.append(f"- {e}")
    else:
        lines.append(f"- fetch: `{git_report.get('fetch','')[:200]}`")
        lines.append(f"- pull:  `{git_report.get('pull','')[:200]}`")

    # 중복 이슈
    lines.append(f"\n## 중복 이슈 그룹 ({len(duplicate_groups)}건)")
    if duplicate_groups:
        for i, group in enumerate(duplicate_groups, 1):
            keeper = group[0]
            lines.append(
                f"{i}. 대표 #{keeper['number']} *{keeper['title']}* ← "
                + ", ".join(f"#{d['number']}" for d in group[1:])
            )
    else:
        lines.append("없음")

    # 자동 종결
    lines.append(f"\n## 자동 종결 이슈 ({len(auto_closed)}건)")
    if auto_closed:
        for issue, reason in auto_closed:
            lines.append(f"- #{issue['number']} *{issue['title']}* — {reason}")
    else:
        lines.append("없음")

    lines.append("\n---\n> 이 이슈는 자동 이슈 관리 에이전트가 생성했습니다.")

    body = "\n".join(lines)
    label_names = ["bot", "issue-management"]

    if not DRY_RUN:
        try:
            api._post("/issues", {
                "title": f"[자동보고] 이슈 관리 결과 {now}",
                "body": body,
                "labels": label_names,
            })
            log.info("요약 이슈 게시 완료")
        except Exception as e:
            log.warning("요약 이슈 게시 실패(라벨 문제일 수 있음): %s, 라벨 없이 재시도...", e)
            try:
                api._post("/issues", {
                    "title": f"[자동보고] 이슈 관리 결과 {now}",
                    "body": body,
                })
                log.info("요약 이슈 게시 완료 (라벨 없음)")
            except Exception as e2:
                log.error("요약 이슈 게시 실패: %s", e2)
    else:
        log.info("[DRY] 요약 이슈:\n%s", body[:500])


# ─────────────────────────────────────────────────────────────────────────────
# 8. GitHub 리포 자동 감지
# ─────────────────────────────────────────────────────────────────────────────

def detect_github_repo(repo_path: str) -> Optional[str]:
    """로컬 git remote에서 owner/repo 추출."""
    ok, out = run_git(["remote", "get-url", "origin"], repo_path)
    if not ok:
        return None
    url = out.strip()
    # SSH: git@github.com:owner/repo.git
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# 9. 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=== GitHub Issue Manager Agent 시작 ===")
    log.info("대상 경로: %s", REPO_PATH)
    log.info("DRY_RUN: %s", DRY_RUN)

    # ── 1. Git 동기화 ──────────────────────────────────────────────────────
    git_report = git_sync(REPO_PATH)

    # ── 2. GitHub 리포 결정 ────────────────────────────────────────────────
    repo = GITHUB_REPO
    if not repo and os.path.isdir(REPO_PATH):
        repo = detect_github_repo(REPO_PATH)
    if not repo:
        log.error(
            "GITHUB_REPO 환경변수를 설정하거나 "
            "REPO_PATH가 유효한 git 리포여야 합니다. 예: owner/repo"
        )
        sys.exit(1)
    log.info("대상 리포: %s", repo)

    # ── 3. GitHub API 초기화 ───────────────────────────────────────────────
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN 환경변수가 필요합니다.")
        sys.exit(1)
    api = GitHubAPI(GITHUB_TOKEN, repo)

    # ── 4. 열린 이슈 수집 ─────────────────────────────────────────────────
    log.info("열린 이슈 수집 중...")
    open_issues = api.list_open_issues()
    log.info("열린 이슈 %d건", len(open_issues))

    # ── 5. 중복 감지 ───────────────────────────────────────────────────────
    log.info("중복 이슈 감지 (임계값=%.2f)...", DUPLICATE_THRESHOLD)
    duplicate_groups = detect_duplicates(open_issues, DUPLICATE_THRESHOLD)
    log.info("중복 그룹 %d개 감지", len(duplicate_groups))

    # 이미 중복 처리된 이슈 번호 집합
    dup_issue_numbers = {
        issue["number"]
        for group in duplicate_groups
        for issue in group[1:]  # keeper(group[0])는 제외
    }

    # ── 6. 종결 후보 감지 ─────────────────────────────────────────────────
    log.info("종결 후보 이슈 감지...")
    auto_closed: list[tuple[dict, str]] = []
    for issue in open_issues:
        if issue["number"] in dup_issue_numbers:
            continue
        reason = is_likely_resolved(issue)
        if reason:
            auto_closed.append((issue, reason))
    log.info("자동 종결 후보 %d건", len(auto_closed))

    # ── 7. 이슈 조치 (댓글 + 종결) ────────────────────────────────────────
    if duplicate_groups or auto_closed:
        log.info("이슈 알림 및 조치 실행...")
        notify_on_issues(api, duplicate_groups, auto_closed)
    else:
        log.info("처리할 이슈 없음")

    # ── 8. 요약 이슈 게시 ─────────────────────────────────────────────────
    log.info("요약 보고서 이슈 게시...")
    post_summary_issue(api, git_report, duplicate_groups, auto_closed, repo)

    # ── 9. 담당자별 요약 출력 (참고용) ────────────────────────────────────
    user_summaries = build_user_summaries(duplicate_groups, auto_closed, repo)
    if user_summaries:
        log.info("=== 담당자별 요약 ===")
        for login, summary in user_summaries.items():
            log.info("--- @%s ---\n%s\n", login, summary[:400])

    log.info("=== 완료 ===")
    return {
        "git": git_report,
        "repo": repo,
        "duplicates": len(duplicate_groups),
        "auto_closed": len(auto_closed),
        "notified_users": list(user_summaries.keys()),
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
