#!/usr/bin/env python3
"""
GitHub Issue Management Agent
- git fetch & pull origin master (or GitHub API fallback)
- 중복 이슈 탐지 및 그룹화 (한국어 char n-gram + cosine similarity)
- 종결 키워드/라벨 기반 자동 close
- 담당자(assignee) 및 작성자에게 GitHub 코멘트 알림
- 이전 자동보고 이슈 종결 후 새 보고서 이슈 생성
"""

import os
import subprocess
import json
import re
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
import urllib.request
import urllib.error

# ── 설정 ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
REPO_OWNER        = "minwooking"
REPO_NAME         = "knowledgebuilder"
REPO_PATH         = "/data/workspace/knowledgebuilder"
BASE_BRANCH       = "main"
API_BASE          = "https://api.github.com"

# 한국어 포함 텍스트에 최적화된 임계값
SIMILARITY_THRESHOLD = float(os.environ.get("DUPLICATE_THRESHOLD", "0.35"))

# 종결 판단 키워드
CLOSED_KEYWORDS = [
    "완료", "done", "완결", "해결", "resolved", "fixed", "close", "closed",
    "finish", "finished", "적용완료", "구현완료", "배포완료", "수정완료",
    "merged", "머지됨", "pr머지", "배포", "릴리즈",
]

# 봇 이슈 라벨 (이전 보고서 탐지용)
BOT_LABELS = {"bot", "issue-management"}

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── GitHub API ────────────────────────────────────────────────────────────────

def gh_request(method: str, path: str, body: Optional[dict] = None):
    url = f"{API_BASE}{path}"
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
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        print(f"  [HTTP {e.code}] {method} {path}: {body_txt[:200]}")
        return {}


def get_all_issues(state: str = "open") -> list:
    issues, page = [], 1
    while True:
        batch = gh_request(
            "GET",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues"
            f"?state={state}&per_page=100&page={page}&filter=all",
        )
        if not isinstance(batch, list) or not batch:
            break
        # Pull Request는 제외
        issues.extend(i for i in batch if "pull_request" not in i)
        page += 1
    return issues


def get_recent_commits(n: int = 5) -> list:
    result = gh_request("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/commits?per_page={n}&sha={BASE_BRANCH}")
    return result if isinstance(result, list) else []


def post_comment(number: int, body: str):
    if DRY_RUN:
        print(f"  [DRY] comment on #{number}: {body[:80]}")
        return
    gh_request("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}/comments", {"body": body})


def close_issue(number: int, comment: Optional[str] = None):
    if comment:
        post_comment(number, comment)
    if DRY_RUN:
        print(f"  [DRY] close #{number}")
        return
    gh_request(
        "PATCH",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}",
        {"state": "closed", "state_reason": "completed"},
    )


def ensure_label(name: str, color: str, description: str = ""):
    existing = gh_request("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels?per_page=100")
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh_request("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                   {"name": name, "color": color, "description": description})


def add_label(number: int, labels: list[str]):
    if DRY_RUN:
        print(f"  [DRY] add labels {labels} to #{number}")
        return
    gh_request("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}/labels", {"labels": labels})


def create_issue(title: str, body: str, labels: list[str]) -> dict:
    if DRY_RUN:
        print(f"  [DRY] create issue: {title}")
        return {}
    result = gh_request("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                        {"title": title, "body": body, "labels": labels})
    return result if isinstance(result, dict) else {}

# ── Git 동기화 (로컬 + API 폴백) ─────────────────────────────────────────────

def run_git(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", REPO_PATH] + args, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def git_sync() -> dict:
    report = {"method": None, "fetch": None, "pull": None,
              "latest_commit": None, "recent_commits": [], "errors": []}

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
        report["method"] = "api"
        report["errors"].append(f"로컬 git 저장소 없음 ({REPO_PATH}) — GitHub API 사용")

    # API로 최신 커밋 정보 보강
    commits = get_recent_commits(5)
    if commits:
        latest = commits[0]
        report["latest_commit"] = {
            "sha": latest["sha"][:8],
            "message": latest["commit"]["message"].split("\n")[0],
        }
        report["recent_commits"] = [
            {"sha": c["sha"][:8], "message": c["commit"]["message"].split("\n")[0]}
            for c in commits
        ]

    return report

# ── 텍스트 유사도 (한국어 char n-gram + TF-IDF cosine) ────────────────────────

def char_ngrams(text: str, n: int = 2) -> list[str]:
    text = re.sub(r"\s+", "", text.lower())
    return [text[i:i+n] for i in range(len(text) - n + 1)]


def build_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf: dict[str, float] = defaultdict(float)
    for t in tokens:
        tf[t] += 1
    total = max(len(tokens), 1)
    return {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}


def cosine(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v**2 for v in a.values()))
    nb = math.sqrt(sum(v**2 for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_duplicates(issues: list) -> list[list[dict]]:
    """중복 이슈 그룹 반환 (각 그룹[0]이 대표 이슈)"""
    docs = [f"{i['title']} {i.get('body') or ''}" for i in issues]
    token_lists = [char_ngrams(d, 2) for d in docs]

    # IDF 계산
    N = len(docs)
    df: dict[str, int] = defaultdict(int)
    for tl in token_lists:
        for t in set(tl):
            df[t] += 1
    idf = {t: math.log((N + 1) / (c + 1) + 1) for t, c in df.items()}

    vectors = [build_vector(tl, idf) for tl in token_lists]

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
            if cosine(vectors[i], vectors[j]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    groups: dict[int, list] = defaultdict(list)
    for idx in range(N):
        groups[find(idx)].append(issues[idx])

    result = []
    for g in groups.values():
        if len(g) > 1:
            # 이슈 번호 오름차순 정렬 (낮은 번호 = 원본)
            g.sort(key=lambda x: x["number"])
            result.append(g)
    return result

# ── 종결 키워드 탐지 ─────────────────────────────────────────────────────────

def auto_close_reason(issue: dict) -> Optional[str]:
    text = f"{issue['title']} {issue.get('body') or ''}".lower()
    for kw in CLOSED_KEYWORDS:
        if kw in text:
            return f"키워드 감지: `{kw}`"
    labels = [l["name"].lower() for l in issue.get("labels", [])]
    for lbl in labels:
        if any(kw in lbl for kw in ["done", "complete", "wontfix", "resolved", "fixed"]):
            return f"라벨 감지: `{lbl}`"
    return None

# ── 봇 보고 이슈 탐지 ────────────────────────────────────────────────────────

def is_bot_report(issue: dict) -> bool:
    label_names = {l["name"] for l in issue.get("labels", [])}
    if label_names & BOT_LABELS:
        return True
    title = issue.get("title", "")
    return title.startswith("[자동보고]") or title.startswith("[bot]")

# ── 알림 멘션 ────────────────────────────────────────────────────────────────

def mentions(issue: dict) -> str:
    people = {issue["user"]["login"]}
    for a in issue.get("assignees", []):
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))

# ── 요약 보고서 이슈 생성 ────────────────────────────────────────────────────

def build_report(
    git_report: dict,
    dup_groups: list[list[dict]],
    auto_closed: list[tuple[dict, str]],
    remaining: list[dict],
    bot_closed: list[dict],
) -> tuple[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"[자동보고] 이슈 관리 결과 {now}"

    lines = ["## 이슈 관리 에이전트 실행 결과\n"]
    lines.append(f"> 실행 시각: {now}")
    lines.append(f"> 저장소: [minwooking/knowledgebuilder](https://github.com/minwooking/knowledgebuilder)\n")
    lines.append("---\n")

    # Git 동기화
    lines.append("### Git 동기화")
    if git_report.get("method") == "git":
        lines.append(f"- fetch: `{git_report.get('fetch','')[:120]}`")
        lines.append(f"- pull: `{git_report.get('pull','')[:120]}`")
    else:
        lines.append("- **방식**: GitHub API (로컬 저장소 없음)")
    if git_report.get("latest_commit"):
        lc = git_report["latest_commit"]
        lines.append(f"- 최신 커밋: `{lc['sha']}` — {lc['message']}")
    if git_report.get("recent_commits"):
        lines.append("- 최근 커밋:")
        for c in git_report["recent_commits"]:
            lines.append(f"  - `{c['sha']}` {c['message']}")
    if git_report.get("errors"):
        lines.append("- ⚠️ " + "; ".join(git_report["errors"]))
    lines.append("")

    # 미해결 오픈 이슈
    lines.append(f"### 미해결 오픈 이슈 ({len(remaining)}건)\n")
    if remaining:
        lines.append("| # | 제목 | 분류 | 담당자 | 작성자 |")
        lines.append("|---|------|------|--------|--------|")
        for i in remaining:
            labels = ", ".join(f"`{l['name']}`" for l in i.get("labels", [])) or "—"
            assignees = ", ".join(f"@{a['login']}" for a in i.get("assignees", [])) or "_미할당_"
            lines.append(f"| #{i['number']} | {i['title']} | {labels} | {assignees} | @{i['user']['login']} |")
    else:
        lines.append("없음")
    lines.append("")

    # 처리 내역
    lines.append("### 이번 실행 처리 내역\n")
    lines.append(f"#### 중복 이슈 그룹 ({len(dup_groups)}건)")
    if dup_groups:
        for g in dup_groups:
            keeper = g[0]
            dups = g[1:]
            dup_refs = ", ".join(f"#{d['number']}" for d in dups)
            lines.append(f"- 대표 #{keeper['number']} **{keeper['title']}** ← 중복: {dup_refs}")
    else:
        lines.append("없음")
    lines.append("")

    lines.append(f"#### 자동 종결 이슈 ({len(auto_closed)}건)")
    if auto_closed:
        for iss, reason in auto_closed:
            lines.append(f"- #{iss['number']} {iss['title']} ({reason})")
    else:
        lines.append("없음")
    lines.append("")

    lines.append(f"#### 봇 보고 이슈 정리 ({len(bot_closed)}건)")
    if bot_closed:
        for b in bot_closed:
            lines.append(f"- #{b['number']} 종결")
    else:
        lines.append("없음")
    lines.append("")

    # 조치 필요 항목
    if remaining:
        lines.append("---\n")
        lines.append("### 조치 필요 항목")
        for i in remaining:
            assignees = [a["login"] for a in i.get("assignees", [])]
            mention_str = f"(@{i['user']['login']})" if not assignees else f"({', '.join('@'+a for a in assignees)})"
            lines.append(f"- [ ] **#{i['number']}** {i['title']}: 담당자 확인 필요 {mention_str}")

    lines.append("\n---")
    lines.append("_이 이슈는 자동 이슈 관리 에이전트가 생성하였습니다._")

    return title, "\n".join(lines)

# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 환경변수 없음")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f" GitHub Issue Management Agent")
    print(f" 대상: {REPO_OWNER}/{REPO_NAME}  |  DRY_RUN={DRY_RUN}")
    print(f"{'='*60}\n")

    # 1. Git 동기화
    print("[1] Git 동기화")
    git_report = git_sync()
    if git_report.get("latest_commit"):
        lc = git_report["latest_commit"]
        print(f"  최신 커밋: {lc['sha']} — {lc['message']}")
    for e in git_report.get("errors", []):
        print(f"  ⚠ {e}")

    # 2. 라벨 준비
    print("\n[2] 라벨 준비")
    ensure_label("duplicate", "cfd3d5", "중복 이슈")
    ensure_label("wontfix",   "ffffff", "종결/불필요")
    ensure_label("bot",       "0075ca", "봇 자동 생성")
    ensure_label("issue-management", "e4e669", "이슈 관리")
    print("  done")

    # 3. 이슈 수집
    print("\n[3] 오픈 이슈 수집")
    all_open = get_all_issues("open")
    print(f"  총 {len(all_open)}개")

    # 봇 이슈 / 실제 이슈 분리
    bot_issues = [i for i in all_open if is_bot_report(i)]
    real_issues = [i for i in all_open if not is_bot_report(i)]
    print(f"  실제 이슈: {len(real_issues)}개 | 봇 보고: {len(bot_issues)}개")

    # 4. 중복 탐지 (실제 이슈 대상)
    print("\n[4] 중복 이슈 탐지")
    dup_groups: list[list[dict]] = []
    if len(real_issues) >= 2:
        dup_groups = find_duplicates(real_issues)
    print(f"  중복 그룹: {len(dup_groups)}개")

    dup_secondary_nums: set[int] = {
        issue["number"]
        for g in dup_groups
        for issue in g[1:]
    }

    # 5. 종결 후보 탐지 (중복 처리된 것 제외)
    print("\n[5] 자동 종결 후보 탐지")
    auto_closed: list[tuple[dict, str]] = []
    for issue in real_issues:
        if issue["number"] in dup_secondary_nums:
            continue
        reason = auto_close_reason(issue)
        if reason:
            auto_closed.append((issue, reason))
    print(f"  종결 후보: {len(auto_closed)}개")

    # 6. 중복 이슈 처리
    print("\n[6] 중복 이슈 처리")
    for group in dup_groups:
        keeper = group[0]
        dups = group[1:]
        print(f"  그룹: #{keeper['number']} '{keeper['title']}'")
        for dup in dups:
            m = mentions(dup)
            comment = (
                f"## 중복 이슈 알림\n\n"
                f"{m}\n\n"
                f"이 이슈는 #{keeper['number']} **{keeper['title']}** 과(와) 중복으로 "
                f"판단되어 자동 종결합니다.\n\n"
                f"- 대표 이슈: #{keeper['number']}\n"
                f"- 처리 일시: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"> 잘못 처리된 경우 이슈를 다시 열고 의견을 남겨주세요."
            )
            add_label(dup["number"], ["duplicate"])
            close_issue(dup["number"], comment)
            print(f"    → #{dup['number']} '{dup['title']}' 중복 종결 + 알림")

        # 대표 이슈에 요약 코멘트
        if dups:
            dup_list = "\n".join(f"- #{d['number']} {d['title']}" for d in dups)
            keeper_comment = (
                f"## 중복 이슈 묶음 요약\n\n"
                f"{mentions(keeper)}\n\n"
                f"아래 이슈들이 이 이슈의 중복으로 탐지되어 종결되었습니다:\n{dup_list}"
            )
            post_comment(keeper["number"], keeper_comment)

    # 7. 자동 종결 처리
    print("\n[7] 자동 종결 처리")
    for issue, reason in auto_closed:
        m = mentions(issue)
        comment = (
            f"## 이슈 자동 종결 알림\n\n"
            f"{m}\n\n"
            f"제목/내용/라벨 분석 결과 이 이슈가 **완료**된 것으로 판단됩니다.\n\n"
            f"- 종결 근거: {reason}\n"
            f"- 처리 일시: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"> 잘못 닫힌 경우 이슈를 다시 열고 **wontfix** 라벨을 제거해 주세요."
        )
        close_issue(issue["number"], comment)
        print(f"  → #{issue['number']} '{issue['title']}' 자동 종결 ({reason})")

    # 8. 이전 봇 보고 이슈 종결
    print("\n[8] 이전 봇 보고 이슈 정리")
    for b in bot_issues:
        close_issue(b["number"])
        print(f"  → #{b['number']} '{b['title']}' 종결")

    # 9. 남은 실제 이슈 목록
    auto_closed_nums = {i["number"] for i, _ in auto_closed} | dup_secondary_nums
    remaining = [i for i in real_issues if i["number"] not in auto_closed_nums]

    # 10. 요약 보고서 이슈 생성
    print("\n[9] 요약 보고서 생성")
    title, body = build_report(git_report, dup_groups, auto_closed, remaining, bot_issues)
    new_issue = create_issue(title, body, ["bot", "issue-management"])
    if new_issue.get("html_url"):
        print(f"  → 보고서 이슈 생성: {new_issue['html_url']}")
    else:
        print(f"  → 보고서 제목: {title}")

    # 11. 최종 요약
    print(f"\n{'='*60}")
    print(f" 실행 완료")
    print(f"  실제 이슈: {len(real_issues)}개")
    print(f"  중복 그룹: {len(dup_groups)}개")
    print(f"  자동 종결: {len(auto_closed)}개")
    print(f"  봇 정리:   {len(bot_issues)}개")
    print(f"  잔여 오픈: {len(remaining)}개")
    print(f"{'='*60}\n")

    return {
        "total_open": len(all_open),
        "real_issues": len(real_issues),
        "duplicate_groups": len(dup_groups),
        "auto_closed": len(auto_closed),
        "bot_cleaned": len(bot_issues),
        "remaining_open": len(remaining),
        "report_url": new_issue.get("html_url", ""),
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
