#!/usr/bin/env python3
"""
GitHub 이슈 관리 에이전트 v7
- git fetch & pull origin master/main (SSH 우선, API 폴백)
- 전체 이슈(open + closed) 기반 중복 감지 및 그룹화 (TF-IDF 코사인 유사도)
- 종결 키워드/레이블/stale 기반 자동 close
- 담당자(assignee) 및 이슈 작성자에게 @멘션 댓글 알림
- 마스터 보고서 이슈 1개만 유지 (날짜 무관 upsert)
- 이전 중복 봇 보고서 대량 정리
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

# 마스터 보고서 마커 (v7 단일 마커)
REPORT_MARKER = "<!-- issue-manager-v7 -->"

# 봇 보고서로 판별하는 마커 패턴 (이전 버전 모두 포함)
ANY_MARKER_RE = re.compile(
    r"<!-- issue-manager(-report|-v[0-9]+|-final|-kb)? -->"
)

# 봇 보고서 제목 패턴
BOT_TITLE_RE = re.compile(r"^\[(이슈관리|이슈 관리|자동보고)\]", re.I)

# 종결 키워드
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
        err = e.read().decode()
        print(f"  [HTTP {e.code}] {method} {path}: {err[:200]}")
        return {}


def get_all_issues(state="open"):
    items, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                   params={"state": state, "per_page": 100, "page": page})
        if not isinstance(batch, list) or not batch:
            break
        items.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
        page += 1
    return items


def post_comment(num, body):
    if DRY_RUN:
        print(f"  [DRY] comment #{num}: {body[:80]}...")
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


def ensure_label(name, color, desc=""):
    existing = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                  params={"per_page": 100})
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
           {"name": name, "color": color, "description": desc})


# ── Git 동기화 (SSH 우선) ──────────────────────────────────────────────────────
def run_git(args):
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
    try:
        r = subprocess.run(
            ["git", "-C", REPO_PATH] + args,
            capture_output=True, text=True, timeout=60,
            env=env,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_sync():
    report = {"fetch": "", "pull": "", "branch": "", "errors": [], "mode": "local"}

    if not os.path.isdir(os.path.join(REPO_PATH, ".git")):
        print("  ⚠ 로컬 저장소 없음 → GitHub API 폴백")
        return _git_sync_api()

    print("  git fetch --prune origin ...")
    rc, out = run_git(["fetch", "--prune", "origin"])
    report["fetch"] = out[:300]
    if rc != 0:
        report["errors"].append(f"fetch 실패: {out[:200]}")
        print(f"  ⚠ fetch 실패 → API 폴백 | {out[:100]}")
        return _git_sync_api()

    # master 우선, 없으면 main
    branch = BASE_BRANCH
    print(f"  git pull origin {branch} ...")
    rc, out = run_git(["pull", "origin", branch])
    if rc != 0:
        alt = "main" if branch == "master" else "master"
        print(f"  ⚠ {branch} pull 실패 → {alt} 시도")
        rc2, out2 = run_git(["pull", "origin", alt])
        if rc2 == 0:
            branch, out = alt, out2
        else:
            report["errors"].append(f"pull 실패: {out2[:200]}")
            print(f"  ⚠ pull 모두 실패 → API 폴백")
            return _git_sync_api()

    report.update(pull=out[:300], branch=branch)
    print(f"  ✓ 브랜치: {branch} | {out[:80]}")
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
            sha = c["sha"][:8]
            msg = c["commit"]["message"].split("\n")[0][:80]
            date = c["commit"]["author"]["date"][:10]
            report["pull"] = f"최신 커밋: {sha} ({date}) {msg}"
            report["branch"] = branch
            print(f"  ✓ API | {branch} @ {sha} — {msg}")
        else:
            report["errors"].append("커밋 조회 실패")
    except Exception as e:
        report["errors"].append(str(e))
    return report


# ── 봇 이슈 판별 ──────────────────────────────────────────────────────────────
def is_bot_issue(issue):
    if BOT_TITLE_RE.search(issue.get("title", "")):
        return True
    body = issue.get("body") or ""
    return bool(ANY_MARKER_RE.search(body))


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
    """실제 이슈(봇 제외) 중 중복 그룹 반환. open/closed 모두 포함."""
    real = [i for i in issues if not is_bot_issue(i)]
    if len(real) < 2:
        return []

    texts = [i["title"] + " " + (i.get("body") or "")[:300] for i in real]
    vecs = tfidf_vectors(texts)

    visited, groups = set(), []
    for idx_a, issue_a in enumerate(real):
        if issue_a["number"] in visited:
            continue
        group = [issue_a]
        for idx_b in range(idx_a + 1, len(real)):
            issue_b = real[idx_b]
            if issue_b["number"] in visited:
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
        if is_bot_issue(issue):
            continue
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        if labels & AUTO_CLOSE_LABELS:
            closeable.append((issue, f"레이블: {labels & AUTO_CLOSE_LABELS}"))
            continue
        if RESOLVED_RE.search(issue.get("title", "")):
            closeable.append((issue, "제목에 종결 키워드 포함"))
            continue
        updated = datetime.fromisoformat(
            issue["updated_at"].replace("Z", "+00:00")
        )
        if (now - updated).days > STALE_DAYS:
            closeable.append((issue, f"{STALE_DAYS}일 이상 비활성"))
    return closeable


# ── 담당자/작성자 알림 ─────────────────────────────────────────────────────────
def notify_stakeholders(open_issues, ts, report_num=None):
    notified = []
    report_link = (
        f" | [관리 보고서 #{report_num}](https://github.com/{REPO_OWNER}/{REPO_NAME}/issues/{report_num})"
        if report_num else ""
    )
    for issue in open_issues:
        if is_bot_issue(issue):
            continue
        num = issue["number"]
        author = issue["user"]["login"]
        assignees = [a["login"] for a in issue.get("assignees", [])]
        labels = " ".join(f"`{l['name']}`" for l in issue.get("labels", []))

        if not assignees:
            body = (
                f"## 📬 이슈 현황 알림\n\n"
                f"@{author}\n\n"
                f"이 이슈(#{num})에 아직 담당자가 지정되어 있지 않습니다.\n\n"
                f"| 항목 | 내용 |\n|------|------|\n"
                f"| 생성일 | {issue['created_at'][:10]} |\n"
                f"| 레이블 | {labels or '없음'} |\n"
                f"| 담당자 | ⚠️ 미지정 |\n\n"
                f"담당자 지정 또는 진행 상황을 업데이트해 주세요.{report_link}\n\n"
                f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
            )
            print(f"  📨 #{num} → @{author} (담당자 없음)")
        else:
            mentions = " ".join(f"@{a}" for a in assignees)
            body = (
                f"## 📬 이슈 현황 알림\n\n"
                f"{mentions} @{author}\n\n"
                f"**#{num} {issue['title']}** 이슈가 Open 상태입니다.\n\n"
                f"진행 상황을 업데이트해 주세요.{report_link}\n\n"
                f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
            )
            print(f"  📨 #{num} → {mentions} + @{author}")

        post_comment(num, body)
        notified.append({
            "issue": num,
            "title": issue["title"],
            "recipients": assignees if assignees else [author],
            "type": "assigned" if assignees else "needs-assignee",
        })
    return notified


# ── 마스터 보고서 단일 upsert ──────────────────────────────────────────────────
def find_master_report(open_issues):
    """v7 마커를 가진 유일한 마스터 보고서 이슈를 반환."""
    for issue in open_issues:
        body = issue.get("body") or ""
        if REPORT_MARKER in body:
            return issue
    return None


def cleanup_old_bot_reports(all_open, keep_num, ts):
    """마스터 보고서(keep_num) 외 모든 open 봇 보고서 close."""
    closed = 0
    for issue in all_open:
        if issue["number"] == keep_num:
            continue
        if not is_bot_issue(issue):
            continue
        print(f"  🗑 이전 봇 보고서 #{issue['number']} 정리")
        if not DRY_RUN:
            post_comment(
                issue["number"],
                f"최신 마스터 보고서(#{keep_num})로 통합되어 종결합니다.\n\n"
                f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
            )
            close_issue_api(issue["number"], "not_planned")
        closed += 1
    return closed


def upsert_master_report(open_issues, body, ts, known_num=None):
    """마스터 보고서 이슈 1개만 유지 — 있으면 edit, 없으면 새로 생성.

    known_num: 이미 알고 있는 보고서 번호 (API 캐시 지연으로 재검색 실패 방지).
    """
    title = f"[이슈관리] 통합 정리 보고서 (최신: {ts})"

    # known_num이 주어지면 바로 업데이트 (목록 API body 잘림 문제 우회)
    if known_num:
        print(f"  ♻️  마스터 보고서 업데이트: #{known_num}")
        if not DRY_RUN:
            gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{known_num}",
               {"title": title, "body": body})
        return known_num

    master = find_master_report(open_issues)
    if master:
        num = master["number"]
        print(f"  ♻️  마스터 보고서 업데이트: #{num}")
        if not DRY_RUN:
            gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
               {"title": title, "body": body})
        return num
    else:
        print("  📝 마스터 보고서 신규 생성...")
        if DRY_RUN:
            return 0
        result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                    {"title": title, "body": body,
                     "labels": ["bot", "issue-management"]})
        num = result.get("number", 0)
        print(f"  ✅ #{num} 생성: {result.get('html_url', '')}")
        return num


# ── 보고서 본문 생성 ──────────────────────────────────────────────────────────
def build_report_body(git_report, dup_groups, closed_list, notified, remaining_open, ts):
    real_open = [i for i in remaining_open if not is_bot_issue(i)]

    def dup_section():
        if not dup_groups:
            return "중복 없음\n"
        lines = []
        for g in dup_groups:
            keeper = min(g, key=lambda x: x["number"])
            dups = [i for i in g if i["number"] != keeper["number"]]
            states = " / ".join(
                f"#{d['number']} ({d['state']})" for d in dups
            )
            lines.append(
                f"- **대표 #{keeper['number']}** *{keeper['title']}*  \n"
                f"  중복: {states}"
            )
        return "\n".join(lines) + "\n"

    def close_section():
        if not closed_list:
            return "없음\n"
        rows = "\n".join(
            f"| #{i['number']} | {i['title'][:50]} | {reason} |"
            for i, reason in closed_list
        )
        return f"| # | 제목 | 사유 |\n|---|------|------|\n{rows}\n"

    def open_section():
        if not real_open:
            return "처리 필요 이슈 없음 🎉\n"
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
            return "발송 없음\n"
        rows = "\n".join(
            f"| {', '.join('@'+r for r in n['recipients'])} | #{n['issue']} | {n['type']} |"
            for n in notified
        )
        return f"| 대상 | 이슈 | 유형 |\n|------|------|------|\n{rows}\n"

    git_status = git_report.get("pull") or git_report.get("fetch") or ""
    git_errors = "\n".join(git_report.get("errors", []))
    mode = git_report.get("mode", "?")
    branch = git_report.get("branch", "?")

    return f"""{REPORT_MARKER}
# 📋 이슈 관리 보고서

**업데이트:** {ts}
**모드:** `{mode}` | 브랜치: `{branch}`

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

> 🤖 이슈 관리 에이전트 v7 · Claude Code · {ts}
> 이 이슈는 에이전트 실행 시마다 자동으로 업데이트됩니다.
"""


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    if not GITHUB_TOKEN:
        print("❌ GITHUB_TOKEN 환경변수가 필요합니다.")
        raise SystemExit(1)

    now = datetime.now(KST)
    ts = now.strftime("%Y-%m-%d %H:%M KST")

    print(f"{'='*60}")
    print(f"🚀 이슈 관리 에이전트 v7 | {ts}")
    print(f"   저장소: {REPO_OWNER}/{REPO_NAME}")
    if DRY_RUN:
        print("   ⚠️  DRY RUN 모드")
    print(f"{'='*60}\n")

    # 1. Git 동기화
    print("─── [1/7] Git 동기화")
    git_report = git_sync()

    # 2. 이슈 전체 로드
    print("\n─── [2/7] 이슈 로드")
    all_issues  = get_all_issues("all")
    open_issues = [i for i in all_issues if i["state"] == "open"]
    print(f"  전체: {len(all_issues)} | Open: {len(open_issues)}")

    # 3. 레이블 보장
    ensure_label("duplicate", "cfd3d5", "중복 이슈")
    ensure_label("bot", "0075ca", "봇 자동 생성")
    ensure_label("issue-management", "e4e669", "이슈 관리 자동화")

    # 4. 중복 감지 (open + closed 전체 기준)
    print(f"\n─── [3/7] 중복 이슈 감지 (임계값={SIMILARITY_THRESHOLD})")
    dup_groups = find_duplicate_groups(all_issues)
    dup_closed_nums = set()

    if dup_groups:
        for g in dup_groups:
            keeper = min(g, key=lambda x: x["number"])
            dups = [i for i in g if i["number"] != keeper["number"]]
            nums_str = ", ".join(f"#{i['number']}" for i in g)
            print(f"  그룹: {nums_str} → 대표 #{keeper['number']}")

            # 대표 이슈에 통합 알림
            dup_list_md = "\n".join(
                f"- #{d['number']} {d['title']} ({d['state']})" for d in dups
            )
            post_comment(
                keeper["number"],
                f"## 🔗 중복 이슈 통합 알림\n\n"
                f"@{keeper['user']['login']}\n\n"
                f"아래 이슈들이 이 이슈와 유사 내용으로 중복 감지되어 통합 처리됩니다:\n\n"
                f"{dup_list_md}\n\n"
                f"중복 이슈는 닫히며, 이 이슈(#{keeper['number']})에서 계속 논의해 주세요.\n\n"
                f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
            )

            # 중복 이슈 close
            for dup in dups:
                post_comment(
                    dup["number"],
                    f"## 🚫 중복 이슈 종결\n\n"
                    f"@{dup['user']['login']}\n\n"
                    f"이 이슈는 **#{keeper['number']} {keeper['title']}** 와(과) 중복으로 판정되어 닫힙니다.\n\n"
                    f"- 대표 이슈: #{keeper['number']}\n"
                    f"- 처리 일시: {ts}\n\n"
                    f"잘못 닫힌 경우 이슈를 다시 열어 주세요.\n\n"
                    f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
                )
                if dup["state"] == "open":
                    add_labels(dup["number"], ["duplicate"])
                    close_issue_api(dup["number"], "not_planned")
                    dup_closed_nums.add(dup["number"])
                    print(f"    #{dup['number']} → close (중복)")
    else:
        print("  중복 없음")

    # 5. 자동 종결
    print("\n─── [4/7] 자동 종결 대상 감지")
    auto_closeable = find_auto_closeable(open_issues)
    closed_list = []
    for issue, reason in auto_closeable:
        if issue["number"] in dup_closed_nums:
            continue
        print(f"  #{issue['number']} '{issue['title'][:50]}' → {reason}")
        post_comment(
            issue["number"],
            f"## ✅ 이슈 자동 종결\n\n"
            f"@{issue['user']['login']}\n\n"
            f"이 이슈는 해결된 것으로 판단되어 자동 종결됩니다.\n\n"
            f"- **종결 사유:** {reason}\n"
            f"- **처리 일시:** {ts}\n\n"
            f"재논의가 필요하면 이슈를 다시 열어 주세요.\n\n"
            f"> 🤖 이슈 관리 에이전트 v7 · {ts}"
        )
        close_issue_api(issue["number"], "completed")
        closed_list.append((issue, reason))
    if not auto_closeable:
        print("  해당 없음")

    # 6. 보고서 생성/업데이트 (먼저 해서 report_num을 알림에 활용)
    print("\n─── [5/7] 마스터 보고서 upsert")
    updated_open = get_all_issues("open")
    report_body = build_report_body(
        git_report, dup_groups, closed_list, [], updated_open, ts
    )
    report_num = upsert_master_report(updated_open, report_body, ts)

    # 7. 이전 봇 보고서 정리
    print("\n─── [6/7] 이전 봇 보고서 정리")
    cleaned = cleanup_old_bot_reports(updated_open, report_num, ts)
    if cleaned > 0:
        print(f"  {cleaned}건 정리 완료")
        # 정리 건수 반영하여 보고서 재업데이트 (known_num으로 재검색 없이 바로 업데이트)
        latest_open = get_all_issues("open")
        report_body2 = build_report_body(
            git_report, dup_groups, closed_list, [], latest_open, ts
        )
        upsert_master_report(latest_open, report_body2, ts, known_num=report_num)
    else:
        print("  정리 대상 없음")

    # 8. 잔여 이슈 담당자/작성자 알림
    print("\n─── [7/7] 잔여 이슈 담당자/작성자 알림")
    closed_all_nums = dup_closed_nums | {i["number"] for i, _ in closed_list}
    remaining = [i for i in updated_open if i["number"] not in closed_all_nums]
    notified = notify_stakeholders(remaining, ts, report_num)
    if not notified:
        print("  처리 필요한 이슈 없음")

    # 최종 보고서 업데이트 (알림 내역 반영)
    if notified and report_num:
        final_open = get_all_issues("open")
        final_body = build_report_body(
            git_report, dup_groups, closed_list, notified, final_open, ts
        )
        upsert_master_report(final_open, final_body, ts, known_num=report_num)

    # ── 결과 요약 ──────────────────────────────────────────────────────────────
    total_closed = len(dup_closed_nums) + len(closed_list)
    print(f"\n{'='*60}")
    print(f"✅ 완료 | {ts}")
    print(f"   Git 브랜치:      {git_report.get('branch','?')} ({git_report.get('mode','?')})")
    print(f"   중복 그룹:       {len(dup_groups)}개 ({len(dup_closed_nums)}건 close)")
    print(f"   자동 종결:       {len(closed_list)}건")
    print(f"   봇 보고서 정리:  {cleaned}건")
    print(f"   알림 발송:       {len(notified)}건")
    print(f"   마스터 보고서:   #{report_num}")
    print(f"{'='*60}")

    return {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "git_branch": git_report.get("branch"),
        "git_mode": git_report.get("mode"),
        "git_errors": git_report.get("errors", []),
        "duplicate_groups": len(dup_groups),
        "duplicates_closed": len(dup_closed_nums),
        "auto_closed": len(closed_list),
        "bot_reports_cleaned": cleaned,
        "notified": len(notified),
        "report_issue": report_num,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
