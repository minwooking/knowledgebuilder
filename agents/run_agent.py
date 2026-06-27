#!/usr/bin/env python3
"""
GitHub 이슈 관리 에이전트 v6
- git fetch & pull origin master/main (로컬 git 또는 API 폴백)
- 중복 이슈 감지 및 그룹화 (TF-IDF 코사인 유사도)
- 종결 키워드/레이블 기반 자동 close
- 담당자/작성자에게 @멘션 댓글 알림
- 하루 1개 보고서만 유지 (upsert: 기존 이슈 편집, 없으면 신규 생성)
- 이전 중복 봇 보고서 자동 정리
"""

import os
import re
import math
import json
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── 설정 ─────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
REPO_OWNER   = os.environ.get("REPO_OWNER", "minwooking")
REPO_NAME    = os.environ.get("REPO_NAME", "knowledgebuilder")
REPO_PATH    = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
BASE_BRANCH  = os.environ.get("BASE_BRANCH", "master")
API_BASE     = "https://api.github.com"
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"

SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.55"))
STALE_DAYS           = int(os.environ.get("STALE_DAYS", "30"))

KST = timezone(timedelta(hours=9))

# 봇 보고서 이슈 마커 (본문에 포함되어야 함)
REPORT_MARKER = "<!-- issue-manager-v6 -->"
# 이전 버전 마커 패턴 (정리 대상)
OLD_MARKER_RE = re.compile(r"<!-- issue-manager(-report|-v[0-9]+|-final|-kb)? -->")
# 봇 보고서 제목 패턴
BOT_TITLE_RE = re.compile(r"^\[(이슈관리|이슈 관리|자동보고)\]", re.I)
# 종결 키워드 패턴
RESOLVED_RE = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|"
    r"완료|해결|닫|종결|수정완료|구현완료|배포완료|적용완료)\b",
    re.I,
)
AUTO_CLOSE_LABELS = {"resolved", "wontfix", "invalid", "done", "completed", "fixed"}


# ── GitHub API ────────────────────────────────────────────────────────────────
def gh(method, path, body=None, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
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
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        print(f"  [HTTP {e.code}] {method} {path}: {body_txt[:200]}")
        return {}


def get_all_issues(state="open"):
    items, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                   params={"state": state, "per_page": 100, "page": page})
        if not isinstance(batch, list) or not batch:
            break
        items.extend(i for i in batch if "pull_request" not in i)
        page += 1
    return items


def post_comment(num, body):
    if DRY_RUN:
        print(f"  [DRY] comment #{num}")
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/comments", {"body": body})


def close_issue_api(num, reason="completed"):
    if DRY_RUN:
        print(f"  [DRY] close #{num}")
        return
    gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
       {"state": "closed", "state_reason": reason})


def add_labels(num, labels):
    if DRY_RUN:
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/labels", {"labels": labels})


# ── Git 동기화 ────────────────────────────────────────────────────────────────
def run_git(args):
    try:
        r = subprocess.run(
            ["git", "-C", REPO_PATH] + args,
            capture_output=True, text=True, timeout=60
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_sync():
    report = {"fetch": "", "pull": "", "branch": "", "errors": [], "mode": "local"}

    if not os.path.isdir(os.path.join(REPO_PATH, ".git")):
        print("  ⚠ 로컬 저장소 없음, GitHub API 폴백")
        return _git_sync_api()

    print("  git fetch --prune origin ...")
    rc, out = run_git(["fetch", "--prune", "origin"])
    report["fetch"] = out[:300]
    if rc != 0:
        report["errors"].append(out[:200])
        print("  ⚠ fetch 실패 → API 폴백")
        return _git_sync_api()

    branch = BASE_BRANCH
    rc, out = run_git(["pull", "origin", branch])
    if rc != 0:
        alt = "main" if branch == "master" else "master"
        rc2, out2 = run_git(["pull", "origin", alt])
        if rc2 == 0:
            branch, out = alt, out2
        else:
            report["errors"].append(out2[:200])
    report.update(pull=out[:300], branch=branch)
    print(f"  ✓ {branch}: {out[:80]}")
    return report


def _git_sync_api():
    report = {"fetch": "API", "pull": "", "branch": "", "errors": [], "mode": "api"}
    try:
        repo = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
        branch = repo.get("default_branch", "main")
        commits = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/commits",
                     params={"sha": branch, "per_page": 3})
        if isinstance(commits, list) and commits:
            c = commits[0]
            sha, msg = c["sha"][:8], c["commit"]["message"].split("\n")[0][:80]
            date = c["commit"]["author"]["date"][:10]
            report["pull"] = f"최신 커밋: {sha} ({date}) {msg}"
            report["branch"] = branch
            print(f"  ✓ API | {branch} @ {sha} {msg}")
        else:
            report["errors"].append("커밋 조회 실패")
    except Exception as e:
        report["errors"].append(str(e))
    return report


# ── TF-IDF 유사도 ─────────────────────────────────────────────────────────────
def tokenize(text):
    return re.findall(r"[가-힣a-z0-9]+", text.lower())


def tfidf_vectors(docs):
    tf_list = []
    for doc in docs:
        tokens = tokenize(doc)
        tf = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        total = max(len(tokens), 1)
        tf_list.append({k: v / total for k, v in tf.items()})

    df = defaultdict(int)
    n = len(docs)
    for tf in tf_list:
        for t in tf:
            df[t] += 1

    vecs = []
    for tf in tf_list:
        v = {t: val * math.log((n + 1) / (df[t] + 1)) for t, val in tf.items()}
        vecs.append(v)
    return vecs


def cosine(a, b):
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_duplicate_groups(issues):
    real = [i for i in issues if not BOT_TITLE_RE.search(i["title"])]
    if len(real) < 2:
        return []

    titles = [i["title"] + " " + (i.get("body") or "")[:200] for i in real]
    vecs = tfidf_vectors(titles)

    visited, groups = set(), []
    for idx_a, issue_a in enumerate(real):
        if issue_a["number"] in visited:
            continue
        group = [issue_a]
        for idx_b, issue_b in enumerate(real):
            if idx_b <= idx_a or issue_b["number"] in visited:
                continue
            if cosine(vecs[idx_a], vecs[idx_b]) >= SIMILARITY_THRESHOLD:
                group.append(issue_b)
                visited.add(issue_b["number"])
        if len(group) > 1:
            visited.add(issue_a["number"])
            groups.append(group)
    return groups


# ── 자동 close 대상 ────────────────────────────────────────────────────────────
def find_auto_closeable(open_issues):
    closeable = []
    now = datetime.now(timezone.utc)
    for issue in open_issues:
        if BOT_TITLE_RE.search(issue["title"]):
            continue  # 봇 보고서는 별도 처리
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        title_body = issue["title"] + " " + (issue.get("body") or "")

        if labels & AUTO_CLOSE_LABELS:
            closeable.append((issue, f"레이블: {labels & AUTO_CLOSE_LABELS}"))
        elif RESOLVED_RE.search(issue["title"]):
            closeable.append((issue, "제목에 종결 키워드 포함"))
        else:
            updated = datetime.fromisoformat(
                issue["updated_at"].replace("Z", "+00:00")
            )
            if (now - updated).days > STALE_DAYS:
                closeable.append((issue, f"{STALE_DAYS}일 이상 비활성"))
    return closeable


# ── 담당자/작성자 알림 ─────────────────────────────────────────────────────────
def notify_stakeholders(open_issues, ts):
    notified = []
    real = [i for i in open_issues if not BOT_TITLE_RE.search(i["title"])]
    for issue in real:
        num = issue["number"]
        author = issue["user"]["login"]
        assignees = [a["login"] for a in issue.get("assignees", [])]
        labels = " ".join(f"`{l['name']}`" for l in issue.get("labels", []))

        if not assignees:
            body = (
                f"## 📬 이슈 알림\n\n"
                f"@{author} — 이 이슈(#{num})에 아직 담당자가 지정되어 있지 않습니다.\n\n"
                f"| 항목 | 내용 |\n|------|------|\n"
                f"| 생성일 | {issue['created_at'][:10]} |\n"
                f"| 레이블 | {labels or '없음'} |\n"
                f"| 담당자 | ⚠️ 미지정 |\n\n"
                f"담당자 지정 또는 진행 상황을 업데이트해 주세요.\n\n"
                f"---\n> 🤖 이슈 관리 에이전트 v6 · {ts}"
            )
            print(f"  📨 #{num} → @{author} (담당자 없음)")
        else:
            body = (
                f"## 📬 이슈 현황 알림\n\n"
                + "".join(f"@{a} " for a in assignees)
                + f"\n\n**#{num} {issue['title']}** 이슈가 Open 상태입니다.\n\n"
                f"진행 상황을 업데이트해 주세요.\n\n"
                f"---\n> 🤖 이슈 관리 에이전트 v6 · {ts}"
            )
            print(f"  📨 #{num} → {', '.join('@'+a for a in assignees)}")

        post_comment(num, body)
        notified.append({
            "issue": num,
            "title": issue["title"],
            "recipients": assignees if assignees else [author],
            "type": "assigned" if assignees else "needs-assignee",
        })
    return notified


# ── 봇 보고서 Upsert ──────────────────────────────────────────────────────────
def find_today_report(all_issues, today_str):
    """오늘 날짜의 기존 보고서 이슈를 찾아 반환."""
    for issue in all_issues:
        if not BOT_TITLE_RE.search(issue["title"]):
            continue
        body = issue.get("body") or ""
        # 오늘 날짜와 마커 둘 다 있으면 오늘의 보고서
        if REPORT_MARKER in body and today_str in body:
            return issue
    return None


def cleanup_old_bot_reports(all_issues, keep_issue_num, ts):
    """오늘 날짜를 제외한 이전 봇 보고서 open 이슈 close."""
    closed = 0
    for issue in all_issues:
        if issue["state"] != "open":
            continue
        if issue["number"] == keep_issue_num:
            continue
        if not BOT_TITLE_RE.search(issue["title"]):
            continue
        body = issue.get("body") or ""
        if OLD_MARKER_RE.search(body) or BOT_TITLE_RE.search(issue["title"]):
            print(f"  🗑 이전 보고서 정리: #{issue['number']}")
            post_comment(
                issue["number"],
                f"최신 보고서(#{keep_issue_num})로 통합되어 종결합니다.\n\n"
                f"> 🤖 이슈 관리 에이전트 v6 · {ts}"
            )
            close_issue_api(issue["number"], "not_planned")
            add_labels(issue["number"], ["duplicate"])
            closed += 1
    return closed


def upsert_report(all_issues, body, today_str, ts):
    """오늘의 보고서 이슈를 업데이트하거나 새로 생성."""
    today_title = f"[이슈관리] 통합 정리 보고서 {today_str}"
    existing = find_today_report(all_issues, today_str)

    if existing:
        num = existing["number"]
        print(f"  ♻️  기존 보고서 업데이트: #{num}")
        if not DRY_RUN:
            gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
               {"title": today_title, "body": body, "state": "open"})
        return num, False  # (번호, 신규생성여부)
    else:
        print(f"  📝 새 보고서 생성...")
        if not DRY_RUN:
            result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                       {"title": today_title, "body": body,
                        "labels": ["bot", "issue-management"]})
            num = result.get("number", 0)
            print(f"  ✅ #{num} 생성: {result.get('html_url','')}")
            return num, True
        return 0, True


# ── 보고서 본문 생성 ──────────────────────────────────────────────────────────
def build_report_body(git_report, dup_groups, closed_list, notified,
                      remaining_open, today_str, ts):
    real_open = [i for i in remaining_open if not BOT_TITLE_RE.search(i["title"])]

    def dup_section():
        if not dup_groups:
            return "- 중복 없음\n"
        lines = []
        for g in dup_groups:
            kept = max(g, key=lambda x: x["number"])
            dupes = [i for i in g if i["number"] != kept["number"]]
            lines.append(f"\n**원본 #{kept['number']}** — {kept['title']}")
            for d in dupes:
                state = "✅ closed" if d["state"] == "closed" else "🔄 open"
                lines.append(f"- #{d['number']} {d['title']} → {state}")
        return "\n".join(lines) + "\n"

    def close_section():
        if not closed_list:
            return "- 없음\n"
        rows = "\n".join(
            f"| #{i['number']} | {i['title'][:50]} | {reason} |"
            for i, reason in closed_list
        )
        return f"| # | 제목 | 사유 |\n|---|------|------|\n{rows}\n"

    def open_section():
        if not real_open:
            return "- 처리 필요 이슈 없음 🎉\n"
        parts = []
        for i in real_open:
            assignees = [a["login"] for a in i.get("assignees", [])]
            labels = " ".join(f"`{l['name']}`" for l in i.get("labels", []))
            parts.append(
                f"### #{i['number']} · {i['title']}\n"
                f"- **작성자:** @{i['user']['login']}\n"
                f"- **담당자:** {', '.join('@'+a for a in assignees) or '⚠️ 미지정'}\n"
                f"- **레이블:** {labels or '없음'}\n"
                f"- **생성일:** {i['created_at'][:10]}\n"
            )
        return "\n".join(parts)

    def notify_section():
        if not notified:
            return "- 발송 없음\n"
        rows = "\n".join(
            f"| {', '.join('@'+r for r in n['recipients'])} | #{n['issue']} | {n['type']} |"
            for n in notified
        )
        return f"| 대상 | 이슈 | 유형 |\n|------|------|------|\n{rows}\n"

    git_status = git_report.get("pull", "") or git_report.get("fetch", "")
    git_errors = "\n".join(git_report.get("errors", []))

    return f"""{REPORT_MARKER}
# 📋 이슈 관리 보고서

**날짜:** {ts}
**모드:** {git_report.get('mode','?')} | 브랜치: `{git_report.get('branch','?')}`

---

## 🔄 Git 동기화

```
{git_status}
{git_errors}
```

---

## 🔗 중복 이슈 ({len(dup_groups)}건)

{dup_section()}

---

## ✅ 자동 종결 ({len(closed_list)}건)

{close_section()}

---

## 📌 처리 필요 이슈 ({len(real_open)}건)

{open_section()}

---

## 📨 알림 발송 ({len(notified)}건)

{notify_section()}

---

> 🤖 이슈 관리 에이전트 v6 · Claude Code · {ts}
"""


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    if not GITHUB_TOKEN:
        print("❌ GITHUB_TOKEN 환경변수가 필요합니다.")
        raise SystemExit(1)

    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M KST")

    print(f"{'='*60}")
    print(f"🚀 이슈 관리 에이전트 v6 | {ts}")
    print(f"   저장소: {REPO_OWNER}/{REPO_NAME}")
    if DRY_RUN:
        print("   ⚠️  DRY RUN 모드")
    print(f"{'='*60}\n")

    # 1. Git 동기화
    print("🔄 Git 동기화...")
    git_report = git_sync()

    # 2. 이슈 전체 로드
    print("\n📥 이슈 로드...")
    open_issues  = get_all_issues("open")
    all_issues   = get_all_issues("all")
    print(f"  전체: {len(all_issues)} | Open: {len(open_issues)}")

    # 3. 중복 감지
    print("\n🔍 중복 이슈 감지...")
    dup_groups = find_duplicate_groups(all_issues)
    if dup_groups:
        for g in dup_groups:
            nums = ", ".join(f"#{i['number']}" for i in g)
            print(f"  중복 그룹: {nums}")
            # 가장 최신 이슈를 원본으로 보존, 나머지 close
            kept = max(g, key=lambda x: x["number"])
            for issue in g:
                if issue["number"] != kept["number"] and issue["state"] == "open":
                    post_comment(
                        issue["number"],
                        f"#{kept['number']} 와 중복으로 판정되어 종결합니다.\n\n"
                        f"> 🤖 이슈 관리 에이전트 v6 · {ts}"
                    )
                    add_labels(issue["number"], ["duplicate"])
                    close_issue_api(issue["number"], "not_planned")
    else:
        print("  중복 없음")

    # 4. 자동 종결
    print("\n🔒 자동 종결 대상 감지...")
    auto_close = find_auto_closeable(open_issues)
    closed_list = []
    for issue, reason in auto_close:
        print(f"  ✅ #{issue['number']} → {reason}")
        post_comment(
            issue["number"],
            f"자동 종결 처리합니다.\n**사유:** {reason}\n\n> 🤖 이슈 관리 에이전트 v6 · {ts}"
        )
        close_issue_api(issue["number"], "completed")
        closed_list.append((issue, reason))
    if not auto_close:
        print("  해당 없음")

    # 5. 담당자/작성자 알림
    closed_nums = {i["number"] for i, _ in closed_list}
    remaining_open = [i for i in open_issues if i["number"] not in closed_nums]
    print(f"\n📨 알림 발송 (처리 필요 이슈 {len(remaining_open)}건)...")
    notified = notify_stakeholders(remaining_open, ts)
    if not notified:
        print("  알림 없음")

    # 6. 보고서 Upsert (기존 오늘 보고서 업데이트 or 새 이슈 생성)
    print("\n📝 보고서 Upsert...")
    report_body = build_report_body(
        git_report, dup_groups, closed_list, notified, remaining_open, today_str, ts
    )
    report_num, is_new = upsert_report(all_issues, report_body, today_str, ts)

    # 7. 이전 봇 보고서 정리 (오늘 보고서 제외)
    if report_num:
        print(f"\n🗑 이전 봇 보고서 정리...")
        cleaned = cleanup_old_bot_reports(all_issues, report_num, ts)
        print(f"  {cleaned}건 정리")

    print(f"\n{'='*60}")
    print(f"✅ 완료 | {ts}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
