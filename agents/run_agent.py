#!/usr/bin/env python3
"""
GitHub Issue Management Agent v2
- git fetch & pull origin master (로컬 git 또는 API 폴백)
- 중복 이슈 감지 및 그룹화 (한국어 char n-gram TF-IDF)
- 종결 키워드/라벨 기반 자동 close
- 담당자(assignee) 및 이슈 작성자에게 이슈 코멘트 @멘션 알림
- 하루 1회만 요약 보고서 이슈 생성 (중복 생성 방지)
- 이전 봇 보고서 이슈 자동 정리
"""

import os
import subprocess
import json
import re
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.request
import urllib.error

# ── 설정 ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
REPO_OWNER         = os.environ.get("REPO_OWNER", "minwooking")
REPO_NAME          = os.environ.get("REPO_NAME", "knowledgebuilder")
REPO_PATH          = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
BASE_BRANCH        = os.environ.get("BASE_BRANCH", "main")
API_BASE           = "https://api.github.com"

# 한국어 char n-gram 기반 유사도 임계값 (0.30~0.45 권장)
SIMILARITY_THRESHOLD = float(os.environ.get("DUPLICATE_THRESHOLD", "0.35"))

# N일 이상 미활동 → stale(종결 후보)
STALE_DAYS = int(os.environ.get("STALE_DAYS", "30"))

# true → 실제 이슈 close/댓글 없이 로그만 출력
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# 하루 최대 보고서 이슈 생성 횟수 (중복 방지)
MAX_REPORTS_PER_DAY = int(os.environ.get("MAX_REPORTS_PER_DAY", "1"))

# 종결 판단 키워드
CLOSED_KEYWORDS = [
    "완료", "done", "완결", "해결", "resolved", "fixed", "close", "closed",
    "finish", "finished", "적용완료", "구현완료", "배포완료", "수정완료",
    "merged", "머지됨", "pr머지", "배포", "릴리즈",
]

# 봇 이슈 식별용 라벨
BOT_LABELS = {"bot", "issue-management"}

NOW_UTC = datetime.now(timezone.utc)

# ── GitHub API 헬퍼 ─────────────────────────────────────────────────────────

def gh_request(method: str, path: str, body: Optional[dict] = None,
               full_url: Optional[str] = None):
    url = full_url or f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            next_url = None
            link_header = resp.headers.get("Link", "")
            if 'rel="next"' in link_header:
                m = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                if m:
                    next_url = m.group(1)
            result = json.loads(raw) if raw.strip() else {}
            return result, next_url
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        print(f"  [HTTP {e.code}] {method} {path}: {body_txt[:200]}")
        return {}, None


def gh_get_all(path: str) -> list:
    """페이지네이션 처리하여 전체 목록 반환."""
    results = []
    url = f"{API_BASE}{path}"
    while url:
        data, next_url = gh_request("GET", "", full_url=url)
        if isinstance(data, list):
            results.extend(data)
        url = next_url
    return results


def post_comment(number: int, body: str):
    if DRY_RUN:
        print(f"  [DRY] comment #{number}: {body[:80]}")
        return
    gh_request("POST",
               f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}/comments",
               {"body": body})


def close_issue(number: int, comment: Optional[str] = None):
    if comment:
        post_comment(number, comment)
    if DRY_RUN:
        print(f"  [DRY] close #{number}")
        return
    gh_request("PATCH",
               f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}",
               {"state": "closed", "state_reason": "completed"})


def add_labels(number: int, labels: list):
    if DRY_RUN:
        print(f"  [DRY] add labels {labels} to #{number}")
        return
    gh_request("POST",
               f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}/labels",
               {"labels": labels})


def ensure_label(name: str, color: str, description: str = ""):
    existing, _ = gh_request("GET",
                              f"/repos/{REPO_OWNER}/{REPO_NAME}/labels?per_page=100")
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh_request("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                   {"name": name, "color": color, "description": description})


def create_issue(title: str, body: str, labels: list) -> dict:
    if DRY_RUN:
        print(f"  [DRY] create issue: {title}")
        return {}
    result, _ = gh_request("POST",
                            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                            {"title": title, "body": body, "labels": labels})
    return result if isinstance(result, dict) else {}


def get_open_issues() -> list:
    issues = gh_get_all(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues?state=open&per_page=100"
    )
    return [i for i in issues if "pull_request" not in i]


def get_recent_commits(n: int = 5) -> list:
    result, _ = gh_request(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/commits?per_page={n}&sha={BASE_BRANCH}"
    )
    return result if isinstance(result, list) else []


# ── Git 동기화 ───────────────────────────────────────────────────────────────

def run_git(args: list) -> tuple:
    r = subprocess.run(
        ["git", "-C", REPO_PATH] + args,
        capture_output=True, text=True
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def git_sync() -> dict:
    report = {
        "method": "api",
        "fetch": None, "pull": None,
        "latest_commit": None, "recent_commits": [],
        "errors": []
    }

    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        report["method"] = "git"
        rc, out = run_git(["fetch", "--prune", "origin"])
        report["fetch"] = out or "ok"
        if rc != 0:
            report["errors"].append(f"fetch: {out}")

        for branch in [BASE_BRANCH, "master", "main"]:
            rc, out = run_git(["pull", "origin", branch])
            if rc == 0:
                report["pull"] = out[:200] or "ok"
                break
            if "couldn't find remote ref" not in out:
                report["errors"].append(f"pull {branch}: {out}")
    else:
        report["errors"].append(f"로컬 저장소 없음({REPO_PATH}) — GitHub API 사용")

    # API로 최신 커밋 정보 보강
    commits = get_recent_commits(5)
    if commits:
        lc = commits[0]
        report["latest_commit"] = {
            "sha": lc["sha"][:8],
            "message": lc["commit"]["message"].split("\n")[0],
            "date": lc["commit"]["author"]["date"][:16],
        }
        report["recent_commits"] = [
            {
                "sha": c["sha"][:8],
                "message": c["commit"]["message"].split("\n")[0],
                "date": c["commit"]["author"]["date"][:10],
            }
            for c in commits
        ]

    return report


# ── 텍스트 유사도 (한국어 char 2-gram TF-IDF cosine) ────────────────────────

def char_ngrams(text: str, n: int = 2) -> list:
    text = re.sub(r"\s+", "", text.lower())
    return [text[i:i+n] for i in range(len(text) - n + 1)]


def build_tfidf_vector(tokens: list, idf: dict) -> dict:
    tf: dict = defaultdict(float)
    for t in tokens:
        tf[t] += 1
    total = max(len(tokens), 1)
    return {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}


def cosine_sim(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v**2 for v in a.values()))
    nb = math.sqrt(sum(v**2 for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_duplicates(issues: list) -> list:
    """중복 이슈 그룹 반환. 각 그룹의 [0]이 대표(원본) 이슈."""
    if len(issues) < 2:
        return []

    docs = [f"{i['title']} {i.get('body') or ''}" for i in issues]
    token_lists = [char_ngrams(d, 2) for d in docs]

    N = len(docs)
    df: dict = defaultdict(int)
    for tl in token_lists:
        for t in set(tl):
            df[t] += 1
    idf = {t: math.log((N + 1) / (c + 1) + 1) for t, c in df.items()}
    vectors = [build_tfidf_vector(tl, idf) for tl in token_lists]

    parent = list(range(N))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(N):
        for j in range(i + 1, N):
            if cosine_sim(vectors[i], vectors[j]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    groups: dict = defaultdict(list)
    for idx in range(N):
        groups[find(idx)].append(issues[idx])

    result = []
    for g in groups.values():
        if len(g) > 1:
            g.sort(key=lambda x: x["number"])
            result.append(g)
    return result


# ── 종결 후보 탐지 ────────────────────────────────────────────────────────────

RESOLVED_RE = re.compile(
    r"(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|완료|해결|종결|수정완료|배포완료)",
    re.IGNORECASE,
)


def auto_close_reason(issue: dict) -> Optional[str]:
    title = issue.get("title", "").lower()
    body = (issue.get("body") or "").lower()

    for kw in CLOSED_KEYWORDS:
        if kw in title or kw in body:
            return f"키워드 감지: `{kw}`"

    labels = [l["name"].lower() for l in issue.get("labels", [])]
    for lbl in labels:
        if any(kw in lbl for kw in ["done", "complete", "wontfix", "resolved", "fixed"]):
            return f"라벨 감지: `{lbl}`"

    updated_at = issue.get("updated_at", "")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            delta = NOW_UTC - updated
            if delta.days >= STALE_DAYS:
                return f"{delta.days}일 미활동 (stale)"
        except ValueError:
            pass

    return None


# ── 유틸리티 ────────────────────────────────────────────────────────────────

def is_bot_report(issue: dict) -> bool:
    label_names = {l["name"] for l in issue.get("labels", [])}
    if label_names & BOT_LABELS:
        return True
    title = issue.get("title", "")
    return title.startswith("[자동보고]") or title.startswith("[이슈 관리]") or title.startswith("[bot]")


def mentions(issue: dict) -> str:
    """이슈 작성자 + assignees @멘션 문자열."""
    people = {issue["user"]["login"]}
    for a in issue.get("assignees", []) or []:
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))


def today_report_exists(open_issues: list, closed_issues_today: list) -> bool:
    """오늘 날짜의 봇 보고서 이슈가 이미 MAX_REPORTS_PER_DAY개 이상 생성됐는지 확인."""
    today_str = NOW_UTC.strftime("%Y-%m-%d")
    all_issues = open_issues + closed_issues_today
    count = sum(
        1 for i in all_issues
        if is_bot_report(i)
        and i.get("created_at", "")[:10] == today_str
    )
    return count >= MAX_REPORTS_PER_DAY


def get_today_closed_bot_issues() -> list:
    """오늘 닫힌 봇 이슈 조회 (중복 보고서 방지용)."""
    today_str = NOW_UTC.strftime("%Y-%m-%d")
    since = NOW_UTC.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    closed = gh_get_all(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues"
        f"?state=closed&per_page=100&since={since}"
    )
    return [
        i for i in closed
        if "pull_request" not in i
        and is_bot_report(i)
        and i.get("created_at", "")[:10] == today_str
    ]


# ── 담당자별 개인 알림 코멘트 ────────────────────────────────────────────────

def notify_person_on_issue(issue: dict, subject: str, detail: str):
    """이슈에 담당자/작성자 @멘션 알림 코멘트 게시."""
    m = mentions(issue)
    comment = (
        f"{subject}\n\n"
        f"{m}\n\n"
        f"{detail}\n\n"
        f"_처리 일시: {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}_"
    )
    post_comment(issue["number"], comment)


# ── 요약 보고서 이슈 생성 ─────────────────────────────────────────────────────

def build_report(
    git_report: dict,
    dup_groups: list,
    auto_closed: list,
    remaining: list,
    bot_cleaned: list,
) -> tuple:
    now_str = NOW_UTC.strftime("%Y-%m-%d %H:%M UTC")
    title = f"[이슈 관리] 정리 보고서 {NOW_UTC.strftime('%Y-%m-%d')}"

    lines = [f"# 📋 이슈 관리 보고서\n", f"> 실행 시각: {now_str}"]
    lines.append(f"> 저장소: [{REPO_OWNER}/{REPO_NAME}](https://github.com/{REPO_OWNER}/{REPO_NAME})\n")
    lines.append("---\n")

    # Git 동기화
    lines.append("## 1. Git 동기화")
    method = git_report.get("method", "api")
    if method == "git":
        lines.append(f"- fetch: `{git_report.get('fetch','')[:150]}`")
        lines.append(f"- pull:  `{git_report.get('pull','')[:150]}`")
    else:
        lines.append("- 방식: **GitHub API** (로컬 저장소 없음 — Actions 체크아웃 사용)")

    if lc := git_report.get("latest_commit"):
        lines.append(f"- 최신 커밋: `{lc['sha']}` `{lc['date']}` — {lc['message']}")

    if git_report.get("recent_commits"):
        lines.append("- 최근 커밋 목록:")
        for c in git_report["recent_commits"]:
            lines.append(f"  - `{c['sha']}` ({c['date']}) {c['message']}")

    if git_report.get("errors"):
        lines.append("- ⚠️ " + "; ".join(git_report["errors"]))
    lines.append("")

    # 중복 이슈
    lines.append(f"## 2. 중복 이슈 그룹 ({len(dup_groups)}건)")
    if dup_groups:
        for g in dup_groups:
            keeper = g[0]
            dups = g[1:]
            dup_refs = ", ".join(f"#{d['number']}" for d in dups)
            lines.append(
                f"- 대표 #{keeper['number']} **{keeper['title']}**"
                f" ← 중복 종결: {dup_refs}"
            )
    else:
        lines.append("없음")
    lines.append("")

    # 자동 종결
    lines.append(f"## 3. 자동 종결 이슈 ({len(auto_closed)}건)")
    if auto_closed:
        for iss, reason in auto_closed:
            lines.append(f"- #{iss['number']} {iss['title']} — _{reason}_")
    else:
        lines.append("없음")
    lines.append("")

    # 봇 이슈 정리
    lines.append(f"## 4. 봇 보고서 이슈 정리 ({len(bot_cleaned)}건)")
    if bot_cleaned:
        for b in bot_cleaned:
            lines.append(f"- #{b['number']} _{b['title']}_")
    else:
        lines.append("없음")
    lines.append("")

    # 잔여 오픈 이슈
    lines.append(f"## 5. 미해결 오픈 이슈 ({len(remaining)}건)")
    if remaining:
        lines.append("| # | 제목 | 라벨 | 담당자 | 작성자 |")
        lines.append("|---|------|------|--------|--------|")
        for i in remaining:
            lbls = " ".join(f"`{l['name']}`" for l in i.get("labels", [])) or "—"
            asgn = " ".join(f"@{a['login']}" for a in i.get("assignees", []) or []) or "_미할당_"
            lines.append(
                f"| #{i['number']} | {i['title']} | {lbls} | {asgn} | @{i['user']['login']} |"
            )

        lines.append("\n### 조치 필요 항목")
        for i in remaining:
            asgn = [a["login"] for a in i.get("assignees", []) or []]
            who = ", ".join(f"@{a}" for a in asgn) if asgn else f"@{i['user']['login']}"
            lines.append(f"- [ ] **#{i['number']}** {i['title']} — 담당: {who}")
    else:
        lines.append("없음")

    lines.append("\n---")
    lines.append("_이 이슈는 자동 이슈 관리 에이전트(v2)가 생성하였습니다._")

    return title, "\n".join(lines)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 환경변수 없음")
        sys.exit(1)

    bar = "=" * 60
    print(f"\n{bar}")
    print(f"  GitHub Issue Management Agent v2")
    print(f"  대상: {REPO_OWNER}/{REPO_NAME}  |  DRY_RUN={DRY_RUN}")
    print(f"  실행: {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{bar}\n")

    # 1. Git 동기화
    print("[1] Git 동기화")
    git_report = git_sync()
    if git_report.get("latest_commit"):
        lc = git_report["latest_commit"]
        print(f"  최신 커밋: {lc['sha']} ({lc['date']}) — {lc['message']}")
    for e in git_report.get("errors", []):
        print(f"  ⚠ {e}")

    # 2. 라벨 준비
    print("\n[2] 라벨 준비")
    ensure_label("duplicate",        "cfd3d5", "중복 이슈")
    ensure_label("wontfix",          "ffffff", "종결/불필요")
    ensure_label("bot",              "0075ca", "봇 자동 생성")
    ensure_label("issue-management", "e4e669", "이슈 관리")
    print("  done")

    # 3. 오픈 이슈 수집
    print("\n[3] 오픈 이슈 수집")
    all_open = get_open_issues()
    bot_issues   = [i for i in all_open if is_bot_report(i)]
    real_issues  = [i for i in all_open if not is_bot_report(i)]
    print(f"  전체: {len(all_open)}개 (실제: {len(real_issues)}, 봇보고: {len(bot_issues)})")

    # 4. 보고서 중복 방지 확인
    today_closed_bot = get_today_closed_bot_issues()
    already_reported = today_report_exists(bot_issues, today_closed_bot)
    print(f"\n[4] 오늘 보고서 생성 여부: {'이미 생성됨' if already_reported else '미생성'}")

    # 5. 중복 이슈 감지
    print(f"\n[5] 중복 이슈 감지 (임계값={SIMILARITY_THRESHOLD})")
    dup_groups: list = []
    if len(real_issues) >= 2:
        dup_groups = find_duplicates(real_issues)
    print(f"  중복 그룹: {len(dup_groups)}개")

    dup_secondary_nums: set = {
        issue["number"]
        for g in dup_groups
        for issue in g[1:]
    }

    # 6. 종결 후보 감지
    print("\n[6] 종결 후보 감지")
    auto_closed: list = []
    for issue in real_issues:
        if issue["number"] in dup_secondary_nums:
            continue
        reason = auto_close_reason(issue)
        if reason:
            auto_closed.append((issue, reason))
    print(f"  종결 후보: {len(auto_closed)}개")

    # 7. 중복 이슈 처리
    print("\n[7] 중복 이슈 처리")
    for group in dup_groups:
        keeper = group[0]
        dups   = group[1:]
        print(f"  대표 #{keeper['number']} '{keeper['title']}'")

        # 중복 이슈마다 작성자/담당자에게 알림 + close
        for dup in dups:
            subject = "## 🔄 중복 이슈 자동 종결 알림"
            detail = (
                f"이 이슈는 #{keeper['number']} **{keeper['title']}** 과 중복으로 "
                f"판단되어 자동 종결됩니다.\n\n"
                f"- **대표 이슈**: #{keeper['number']} — {keeper['html_url']}\n"
                f"- **사유**: 제목/본문 유사도 ≥ {SIMILARITY_THRESHOLD}\n\n"
                f"재논의가 필요하면 대표 이슈에 코멘트 부탁드립니다."
            )
            notify_person_on_issue(dup, subject, detail)
            add_labels(dup["number"], ["duplicate"])
            close_issue(dup["number"])
            print(f"    → #{dup['number']} '{dup['title']}' 중복 종결")

        # 대표 이슈에 요약 코멘트
        if dups:
            dup_list = "\n".join(f"- #{d['number']} {d['title']}" for d in dups)
            keeper_msg = (
                f"## 🔗 중복 이슈 묶음 알림\n\n"
                f"{mentions(keeper)}\n\n"
                f"아래 이슈들이 이 이슈와 중복으로 감지되어 종결되었습니다:\n{dup_list}\n\n"
                f"이 이슈에서 계속 진행해 주세요."
            )
            post_comment(keeper["number"], keeper_msg)

    # 8. 자동 종결 처리
    print("\n[8] 자동 종결 처리")
    for issue, reason in auto_closed:
        subject = "## ✅ 이슈 자동 종결 알림"
        detail = (
            f"제목/본문/라벨 분석 결과 **완료**된 것으로 판단되어 자동 종결합니다.\n\n"
            f"- **종결 근거**: {reason}\n\n"
            f"잘못 닫힌 경우 이슈를 다시 열고 코멘트 남겨주세요."
        )
        notify_person_on_issue(issue, subject, detail)
        close_issue(issue["number"])
        print(f"  → #{issue['number']} '{issue['title']}' 자동 종결 ({reason})")

    # 9. 이전 봇 보고서 정리 (오픈 상태인 것만)
    print("\n[9] 봇 보고서 정리")
    for b in bot_issues:
        close_issue(b["number"])
        print(f"  → #{b['number']} '{b['title']}' 정리")

    # 10. 잔여 실제 오픈 이슈
    all_processed_nums = (
        dup_secondary_nums
        | {i["number"] for i, _ in auto_closed}
        | {b["number"] for b in bot_issues}
    )
    remaining = [i for i in real_issues if i["number"] not in all_processed_nums]

    # 11. 잔여 이슈 담당자에게 상태 알림 (변경 사항 있을 때만)
    if (dup_groups or auto_closed) and remaining:
        print("\n[10] 잔여 이슈 담당자 알림")
        for issue in remaining:
            subject = "## 📌 이슈 관리 결과 알림"
            detail = (
                f"이번 이슈 관리 실행 결과 이 이슈는 **미해결 상태**로 남아 있습니다.\n\n"
                f"- 이슈: #{issue['number']} {issue['title']}\n"
                f"- 링크: {issue['html_url']}\n\n"
                f"진행 상황 업데이트 또는 담당자 지정 부탁드립니다."
            )
            notify_person_on_issue(issue, subject, detail)
            print(f"  → #{issue['number']} 잔여 이슈 알림")

    # 12. 요약 보고서 이슈 생성 (하루 MAX_REPORTS_PER_DAY 회 제한)
    print("\n[11] 요약 보고서 이슈 생성")
    new_issue_url = ""
    if already_reported and not (dup_groups or auto_closed):
        print(f"  ℹ️  오늘({NOW_UTC.strftime('%Y-%m-%d')}) 보고서가 이미 생성됨 — 건너뜀")
    else:
        report_title, report_body = build_report(
            git_report, dup_groups, auto_closed, remaining, bot_issues
        )
        new_issue = create_issue(report_title, report_body, ["bot", "issue-management"])
        new_issue_url = new_issue.get("html_url", "")
        if new_issue_url:
            print(f"  → 보고서 이슈 생성: {new_issue_url}")
            # 생성 직후 close (기록용 이슈)
            if new_issue.get("number") and not DRY_RUN:
                close_issue(new_issue["number"])
                print(f"  → #{new_issue['number']} 보고서 이슈 closed (기록 완료)")
        else:
            print(f"  → 보고서: {report_title}")

    # 최종 요약
    print(f"\n{bar}")
    print(f"  실행 완료  |  {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  실제 이슈: {len(real_issues)}개")
    print(f"  중복 그룹: {len(dup_groups)}개")
    print(f"  자동 종결: {len(auto_closed)}개")
    print(f"  봇 정리:   {len(bot_issues)}개")
    print(f"  잔여 오픈: {len(remaining)}개")
    if new_issue_url:
        print(f"  보고서:    {new_issue_url}")
    print(f"{bar}\n")

    return {
        "real_issues": len(real_issues),
        "duplicate_groups": len(dup_groups),
        "auto_closed": len(auto_closed),
        "bot_cleaned": len(bot_issues),
        "remaining_open": len(remaining),
        "report_url": new_issue_url,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
