#!/usr/bin/env python3
"""
GitHub Issue Management Agent v3
- git fetch & pull origin main (로컬 git 또는 API 폴백)
- Claude AI 시맨틱 중복 이슈 감지 (TF-IDF 1차 → Claude CLI 2차 확인)
- 종결 키워드/라벨/stale 기반 자동 close
- 담당자(assignee) 및 이슈 작성자에게 이슈 코멘트 @멘션 알림
- 하루 1개 보고서 이슈만 유지 (upsert 방식, 중복 생성 방지)
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

SIMILARITY_THRESHOLD = float(os.environ.get("DUPLICATE_THRESHOLD", "0.35"))
STALE_DAYS           = int(os.environ.get("STALE_DAYS", "30"))
DRY_RUN              = os.environ.get("DRY_RUN", "false").lower() == "true"
USE_CLAUDE_AI        = os.environ.get("USE_CLAUDE_AI", "true").lower() == "true"

REPORT_MARKER = "<!-- kb-issue-agent-v3-report -->"

CLOSED_KEYWORDS = [
    "완료", "done", "완결", "해결", "resolved", "fixed", "close", "closed",
    "finish", "finished", "적용완료", "구현완료", "배포완료", "수정완료",
    "merged", "머지됨", "pr머지", "배포", "릴리즈",
]

BOT_LABELS = {"bot", "issue-management"}

NOW_UTC = datetime.now(timezone.utc)
NOW_KST = NOW_UTC + timedelta(hours=9)

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


# ── TF-IDF 한국어 char 2-gram 유사도 ─────────────────────────────────────────

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


def find_dup_candidates(issues: list) -> list:
    """TF-IDF로 중복 후보 그룹을 반환. 각 그룹[0]이 대표 이슈."""
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


# ── Claude AI 중복 확인 ──────────────────────────────────────────────────────

def verify_duplicates_with_claude(candidate_groups: list, all_issues: list) -> list:
    """TF-IDF 후보 그룹을 Claude CLI로 시맨틱 검증. 확인된 그룹만 반환."""
    if not candidate_groups:
        return []

    # Claude CLI 존재 여부 확인
    try:
        r = subprocess.run(["which", "claude"], capture_output=True, timeout=5)
        if r.returncode != 0:
            print("  Claude CLI 없음 — TF-IDF 결과만 사용")
            return candidate_groups
    except Exception:
        return candidate_groups

    confirmed = []
    for group in candidate_groups:
        issues_desc = "\n".join(
            f"  #{i['number']}: {i['title']}\n  내용: {(i.get('body') or '')[:200]}"
            for i in group
        )
        prompt = (
            f"다음 GitHub 이슈들이 중복인지 판단해주세요.\n\n"
            f"{issues_desc}\n\n"
            f"이슈들이 실질적으로 같은 문제를 다루면 'YES', 그렇지 않으면 'NO'만 답하세요.\n"
            f"판단 기준: 제목과 내용이 같은 기능 요구사항이나 버그를 다루는가."
        )
        try:
            r = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=30
            )
            answer = r.stdout.strip().upper()
            if "YES" in answer:
                confirmed.append(group)
                print(f"  Claude 확인: #{group[0]['number']} 그룹 → 중복")
            else:
                print(f"  Claude 거부: #{group[0]['number']} 그룹 → 중복 아님")
        except Exception as e:
            print(f"  Claude 오류: {e} — TF-IDF 결과 유지")
            confirmed.append(group)

    return confirmed


def analyze_resolved_with_claude(issues: list) -> list:
    """Claude CLI로 해결됐지만 아직 열린 이슈를 찾아 (issue, reason) 반환."""
    if not issues or not USE_CLAUDE_AI:
        return []

    try:
        r = subprocess.run(["which", "claude"], capture_output=True, timeout=5)
        if r.returncode != 0:
            return []
    except Exception:
        return []

    issues_text = "\n".join(
        f"[#{i['number']}] {i['title']}\n내용: {(i.get('body') or '')[:300]}"
        for i in issues
    )

    prompt = (
        f"다음 GitHub 오픈 이슈들을 분석해주세요:\n\n{issues_text}\n\n"
        f"제목이나 내용에 '해결', '완료', 'resolved', 'fixed', '수정완료' 등이 포함되어 "
        f"이미 해결된 것으로 보이는 이슈 번호만 JSON 배열로 반환하세요.\n"
        f"예: [7, 12] 또는 해당 없으면 []\n"
        f"JSON 배열만 반환하고 다른 텍스트는 없어야 합니다."
    )

    try:
        r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=45)
        match = re.search(r'\[[\d,\s]*\]', r.stdout)
        if match:
            nums = json.loads(match.group())
            issue_map = {i["number"]: i for i in issues}
            return [
                (issue_map[n], "Claude AI: 이슈 내용에서 해결 완료 감지")
                for n in nums if n in issue_map
            ]
    except Exception as e:
        print(f"  Claude 해결 감지 오류: {e}")

    return []


# ── 종결 후보 탐지 ────────────────────────────────────────────────────────────

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
    return any(title.startswith(p) for p in [
        "[자동보고]", "[이슈 관리]", "[이슈관리]", "[bot]"
    ])


def mentions(issue: dict) -> str:
    people = {issue["user"]["login"]}
    for a in issue.get("assignees", []) or []:
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))


def upsert_report_issue(title: str, body: str, labels: list) -> dict:
    """보고서 마커로 기존 오픈 보고서를 찾아 업데이트, 없으면 신규 생성."""
    all_open = get_open_issues()
    for issue in all_open:
        if is_bot_report(issue) and REPORT_MARKER in (issue.get("body") or ""):
            result, _ = gh_request(
                "PATCH",
                f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue['number']}",
                {"title": title, "body": body},
            )
            return {"action": "updated", "number": issue["number"],
                    "html_url": issue.get("html_url", ""), **(result or {})}
    if not DRY_RUN:
        result, _ = gh_request(
            "POST",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
            {"title": title, "body": body, "labels": labels},
        )
        return {"action": "created", **(result or {})}
    return {"action": "dry-run"}


def notify_person_on_issue(issue: dict, subject: str, detail: str):
    m = mentions(issue)
    comment = (
        f"{subject}\n\n"
        f"{m}\n\n"
        f"{detail}\n\n"
        f"_처리 일시: {NOW_KST.strftime('%Y-%m-%d %H:%M KST')}_"
    )
    post_comment(issue["number"], comment)


# ── 보고서 생성 ──────────────────────────────────────────────────────────────

def build_report(git_report, dup_groups, auto_closed, remaining, old_bot_issues) -> tuple:
    ts_kst = NOW_KST.strftime("%Y-%m-%d %H:%M")
    date_kst = NOW_KST.strftime("%Y-%m-%d")
    title = f"[이슈관리] 보고서 {date_kst}"

    lines = [
        REPORT_MARKER,
        f"## 📋 이슈 관리 보고서 — {date_kst}",
        "",
        f"**실행 일시**: {ts_kst} KST",
        f"**레포지토리**: [{REPO_OWNER}/{REPO_NAME}](https://github.com/{REPO_OWNER}/{REPO_NAME})",
        f"**에이전트**: Claude Code Issue Manager v3",
        "",
        "---",
        "",
        "## 🔄 Git 동기화",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
    ]

    if lc := git_report.get("latest_commit"):
        lines += [
            f"| 브랜치 | `{BASE_BRANCH}` |",
            f"| 최신 커밋 | `{lc['sha']}` |",
            f"| 커밋 메시지 | {lc['message']} |",
            f"| 커밋 일시 | {lc['date']} UTC |",
            f"| 동기화 방법 | `{git_report['method']}` |",
        ]
    if git_report.get("errors"):
        lines.append(f"| ⚠️ 오류 | {'; '.join(git_report['errors'])[:150]} |")

    lines += ["", "---", "", "## 🛠️ 이번 실행 처리 내역", "",
              "| 작업 | 건수 |", "|------|------|",
              f"| 중복 이슈 종결 | {sum(len(g)-1 for g in dup_groups)} |",
              f"| 해결 이슈 종결 | {len(auto_closed)} |",
              f"| 담당자/작성자 알림 | {len(remaining)} |",
              f"| 이전 봇 보고서 정리 | {len(old_bot_issues)} |",
              ""]

    if dup_groups:
        lines += ["### 🔁 중복 처리"]
        for g in dup_groups:
            dups = ", ".join(f"#{d['number']}" for d in g[1:])
            lines.append(f"- #{g[0]['number']} **{g[0]['title']}** ← 중복 종결: {dups}")
        lines.append("")

    if auto_closed:
        lines += ["### ✅ 해결 처리"]
        for iss, reason in auto_closed:
            lines.append(f"- #{iss['number']} {iss['title']} — _{reason}_")
        lines.append("")

    lines += ["---", "", "## 📂 미해결 이슈"]
    if remaining:
        lines += ["", "| # | 제목 | 레이블 | 담당자 | 경과 |",
                  "|---|------|--------|--------|------|"]
        for i in remaining:
            lbls = " ".join(f"`{l['name']}`" for l in i.get("labels", [])) or "—"
            asgn = " ".join(f"@{a['login']}" for a in i.get("assignees") or []) or "_미지정_"
            created = datetime.fromisoformat(i["created_at"].replace("Z", "+00:00"))
            days = (NOW_UTC - created).days
            url = i.get("html_url", f"https://github.com/{REPO_OWNER}/{REPO_NAME}/issues/{i['number']}")
            lines.append(f"| #{i['number']} | [{i['title']}]({url}) | {lbls} | {asgn} | {days}일 |")
        lines.append("")
        lines.append("### 조치 필요")
        for i in remaining:
            asgn = [a["login"] for a in i.get("assignees") or []]
            who = ", ".join(f"@{a}" for a in asgn) if asgn else f"@{i['user']['login']}"
            lines.append(f"- [ ] **#{i['number']}** {i['title']} — 담당: {who}")
    else:
        lines.append("_현재 미해결 이슈 없음_")

    lines += ["", "---",
              f"_자동 생성: Claude Code Issue Manager v3 | {ts_kst} KST_"]

    return title, "\n".join(lines)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 환경변수 없음")
        sys.exit(1)

    bar = "=" * 60
    print(f"\n{bar}")
    print(f"  GitHub Issue Management Agent v3")
    print(f"  대상: {REPO_OWNER}/{REPO_NAME}  |  DRY_RUN={DRY_RUN}")
    print(f"  실행: {NOW_KST.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{bar}\n")

    # 1. Git 동기화
    print("[1] Git 동기화")
    git_report = git_sync()
    if lc := git_report.get("latest_commit"):
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
    bot_issues  = [i for i in all_open if is_bot_report(i)]
    real_issues = [i for i in all_open if not is_bot_report(i)]
    print(f"  전체: {len(all_open)} | 실제: {len(real_issues)} | 봇보고: {len(bot_issues)}")

    # 4. 이전 봇 보고서 정리 (REPORT_MARKER 없는 이전 버전 보고서만)
    today_kst_str = NOW_KST.strftime("%Y-%m-%d")
    print("\n[4] 이전 봇 보고서 정리")
    old_bot_issues = []
    for b in bot_issues:
        created_date = b.get("created_at", "")[:10]
        has_v3_marker = REPORT_MARKER in (b.get("body") or "")
        # 오늘 날짜의 v3 마커 보고서는 유지, 나머지 close
        if not has_v3_marker or created_date < today_kst_str:
            if has_v3_marker and created_date == today_kst_str:
                continue  # 오늘 마커 보고서 유지
            old_bot_issues.append(b)
            close_issue(b["number"])
            print(f"  → #{b['number']} 정리: {b['title'][:50]}")
    if not old_bot_issues:
        print("  정리할 이전 보고서 없음")

    # 5. TF-IDF 중복 후보 탐지
    print(f"\n[5] 중복 이슈 탐지 (TF-IDF 임계값={SIMILARITY_THRESHOLD})")
    candidate_groups = find_dup_candidates(real_issues)
    print(f"  TF-IDF 후보 그룹: {len(candidate_groups)}개")

    # 6. Claude AI로 중복 확인
    dup_groups = candidate_groups
    if USE_CLAUDE_AI and candidate_groups:
        print("\n[6] Claude AI 중복 검증")
        dup_groups = verify_duplicates_with_claude(candidate_groups, real_issues)
    else:
        print("\n[6] Claude AI 건너뜀")

    dup_secondary_nums = {
        issue["number"] for g in dup_groups for issue in g[1:]
    }

    # 7. 종결 후보: 키워드/라벨/stale 기반
    print("\n[7] 종결 후보 탐지")
    auto_closed: list = []
    for issue in real_issues:
        if issue["number"] in dup_secondary_nums:
            continue
        reason = auto_close_reason(issue)
        if reason:
            auto_closed.append((issue, reason))

    # Claude AI로 추가 해결 이슈 탐지
    if USE_CLAUDE_AI:
        remaining_open = [
            i for i in real_issues
            if i["number"] not in dup_secondary_nums
            and i not in [ac[0] for ac in auto_closed]
        ]
        ai_resolved = analyze_resolved_with_claude(remaining_open)
        auto_closed.extend(ai_resolved)

    print(f"  종결 후보: {len(auto_closed)}개")

    # 8. 중복 이슈 처리
    print("\n[8] 중복 이슈 처리")
    for group in dup_groups:
        keeper = group[0]
        dups   = group[1:]
        print(f"  대표 #{keeper['number']} '{keeper['title']}'")
        for dup in dups:
            subject = "## 🔄 중복 이슈 자동 종결"
            detail = (
                f"이 이슈는 **#{keeper['number']} {keeper['title']}** 의 중복으로 종결됩니다.\n\n"
                f"- **대표 이슈**: #{keeper['number']} — {keeper.get('html_url','')}\n"
                f"- **탐지 방법**: TF-IDF 유사도 + Claude AI 검증\n\n"
                f"재논의가 필요하면 대표 이슈에 코멘트 부탁드립니다."
            )
            notify_person_on_issue(dup, subject, detail)
            add_labels(dup["number"], ["duplicate"])
            close_issue(dup["number"])
            print(f"    → #{dup['number']} 중복 종결")
        if dups:
            dup_list = "\n".join(f"- #{d['number']} {d['title']}" for d in dups)
            post_comment(keeper["number"],
                f"## 🔗 중복 이슈 통합 완료\n\n{mentions(keeper)}\n\n"
                f"아래 중복 이슈들이 종결되었습니다:\n{dup_list}\n\n"
                f"이 이슈에서 계속 진행해 주세요.")

    # 9. 자동 종결 처리
    print("\n[9] 자동 종결 처리")
    for issue, reason in auto_closed:
        subject = "## ✅ 이슈 자동 종결"
        detail = (
            f"분석 결과 **완료**된 것으로 판단되어 종결합니다.\n\n"
            f"- **종결 근거**: {reason}\n\n"
            f"잘못 닫힌 경우 이슈를 다시 열고 코멘트 남겨주세요."
        )
        notify_person_on_issue(issue, subject, detail)
        close_issue(issue["number"])
        print(f"  → #{issue['number']} '{issue['title']}' 종결 ({reason})")

    # 10. 잔여 오픈 이슈 목록 확정
    auto_closed_nums = {i["number"] for i, _ in auto_closed}
    remaining = [
        i for i in real_issues
        if i["number"] not in dup_secondary_nums
        and i["number"] not in auto_closed_nums
    ]

    # 11. 요약 보고서 upsert (담당자 알림 전에 생성)
    print("\n[11] 요약 보고서 upsert")
    report_title, report_body = build_report(
        git_report, dup_groups, auto_closed, remaining, old_bot_issues
    )
    new_issue = upsert_report_issue(report_title, report_body, ["bot", "issue-management"])
    report_url = new_issue.get("html_url", "")
    action = new_issue.get("action", "")
    print(f"  → 보고서 {action}: #{new_issue.get('number','')} {report_url}")

    # 12. 잔여 오픈 이슈 담당자/작성자 알림
    print(f"\n[12] 잔여 이슈 담당자/작성자 알림 ({len(remaining)}건)")
    today_kst = NOW_KST.strftime("%Y-%m-%d")
    notified_count = 0
    for issue in remaining:
        # 오늘 이미 알림 코멘트를 남겼으면 스킵 (스팸 방지)
        comments, _ = gh_request(
            "GET",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue['number']}/comments"
            f"?per_page=30&sort=updated&direction=desc"
        )
        already_notified = False
        if isinstance(comments, list):
            for c in comments:
                c_login = c.get("user", {}).get("login", "")
                c_body = c.get("body") or ""
                if c_login in (REPO_OWNER, "github-actions[bot]") and today_kst in c_body:
                    already_notified = True
                    break

        if already_notified:
            print(f"  → #{issue['number']} 오늘 이미 알림 발송됨, 스킵")
            continue

        subject = "## 📌 이슈 현황 알림"
        created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
        days = (NOW_UTC - created).days
        asgn = [a["login"] for a in issue.get("assignees") or []]
        detail = (
            f"이 이슈는 현재 **열린 상태 ({days}일째)**입니다.\n\n"
            f"| 항목 | 내용 |\n|------|------|\n"
            f"| 이슈 | #{issue['number']} |\n"
            f"| 제목 | {issue['title']} |\n"
            f"| 담당자 | {', '.join(f'@{a}' for a in asgn) if asgn else '_미지정_'} |\n"
            f"| 보고서 | {report_url} |\n\n"
            f"{'⚠️ 담당자 지정을 권장합니다.' if not asgn else '진행 상황을 업데이트 해주세요.'}"
        )
        notify_person_on_issue(issue, subject, detail)
        notified_count += 1
        print(f"  → #{issue['number']} 알림 발송 (@{issue['user']['login']})")
    if not remaining:
        print("  잔여 이슈 없음")

    # 최종 요약
    print(f"\n{bar}")
    print(f"  실행 완료  |  {NOW_KST.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"  실제 이슈:   {len(real_issues)}개")
    print(f"  중복 종결:   {sum(len(g)-1 for g in dup_groups)}건")
    print(f"  해결 종결:   {len(auto_closed)}건")
    print(f"  봇 정리:     {len(old_bot_issues)}건")
    print(f"  잔여 오픈:   {len(remaining)}건")
    print(f"  알림 발송:   {notified_count}건")
    print(f"  보고서:      {report_url}")
    print(f"{bar}\n")

    return {
        "real_issues": len(real_issues),
        "duplicate_groups": len(dup_groups),
        "auto_closed": len(auto_closed),
        "bot_cleaned": len(old_bot_issues),
        "remaining_open": len(remaining),
        "notified": notified_count,
        "report_url": report_url,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
