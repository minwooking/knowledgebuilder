#!/usr/bin/env python3
"""
GitHub 이슈 관리 에이전트 v10
- git fetch & pull (로컬 경로 → API 폴백)
- 전체 이슈 기반 중복 탐지 & 그룹화 (Jaccard + TF-IDF 하이브리드)
- 종결 키워드/레이블 기반 자동 close
- 담당자(assignee) 및 이슈 작성자에게 @멘션 댓글 알림
- 보고서 이슈 1개만 유지 (이전 보고서 전부 종결)
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
BASE_BRANCH  = os.environ.get("BASE_BRANCH", "main")
API_BASE     = "https://api.github.com"
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"

SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.40"))
STALE_DAYS           = int(os.environ.get("STALE_DAYS", "30"))

KST = timezone(timedelta(hours=9))
now_kst = datetime.now(KST)
now_str = now_kst.strftime("%Y-%m-%d %H:%M KST")

REPORT_MARKER = "<!-- issue-manager-v10 -->"

# 이전 버전 포함 모든 봇 보고서 마커
ANY_MARKER_RE = re.compile(
    r"<!-- (issue-manager|kb-issue-agent)[^\-]*(-v[0-9]+|-report|-final|-v3|-v4|-v5|-v6|-v7|-v8|-v9|-v10)? -->"
)
BOT_TITLE_RE = re.compile(r"^\[(이슈관리|이슈 관리|자동보고)\]", re.I)

# 종결 키워드
RESOLVED_RE = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|"
    r"완료|해결|닫|종결|수정완료|구현완료|배포완료|적용완료|merged?|머지)\b",
    re.I,
)
AUTO_CLOSE_LABELS = {"resolved", "wontfix", "invalid", "done", "completed", "fixed"}

STOP_WORDS = {
    "이", "가", "을", "를", "의", "에", "에서", "로", "으로", "은", "는", "과", "와",
    "이고", "이며", "이나", "이든", "한", "하여", "하고", "합니다", "입니다",
    "a", "an", "the", "in", "on", "at", "is", "of", "for", "and", "or", "to",
    "it", "that", "this", "be", "are", "was", "were",
}

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


def get_all_issues(state="all"):
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
        return True
    result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/comments", {"body": body})
    return bool(result.get("id"))


def close_issue_api(num, reason="completed"):
    if DRY_RUN:
        print(f"  [DRY] close #{num}")
        return True
    result = gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
                {"state": "closed", "state_reason": reason})
    return result.get("state") == "closed"


def add_labels(num, labels):
    if DRY_RUN:
        return
    for label in labels:
        ensure_label(label, "cfd3d7")
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/labels", {"labels": labels})


def ensure_label(name, color="cfd3d7", desc=""):
    existing = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                  params={"per_page": 100})
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
           {"name": name, "color": color, "description": desc})


def create_issue(title, body, labels=None):
    if DRY_RUN:
        print(f"  [DRY] create issue: {title}")
        return {"number": 0, "html_url": "(dry-run)"}
    data = {"title": title, "body": body}
    if labels:
        data["labels"] = labels
    return gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues", data)


# ── 보조 레이블 준비 ──────────────────────────────────────────────────────────
def setup_labels():
    ensure_label("duplicate", "cfd3d7", "중복 이슈")
    ensure_label("bot", "0075ca", "봇이 생성한 이슈")
    ensure_label("issue-management", "e4e669", "이슈 관리 보고서")


# ── Git 동기화 ────────────────────────────────────────────────────────────────
def git_sync():
    result = {"mode": "unknown", "branch": BASE_BRANCH, "status": "", "commit": ""}

    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        result["mode"] = "local"
        for cmd in [
            ["git", "-C", REPO_PATH, "fetch", "--prune", "origin"],
            ["git", "-C", REPO_PATH, "pull", "origin", BASE_BRANCH],
        ]:
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=30)
                result["status"] += out.strip() + "\n"
            except subprocess.CalledProcessError as e:
                result["status"] += f"오류: {e.output.strip()}\n"
            except subprocess.TimeoutExpired:
                result["status"] += "시간 초과\n"
    else:
        result["mode"] = "api (로컬 저장소 없음)"
        try:
            commit = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/commits/{BASE_BRANCH}")
            if commit.get("sha"):
                sha = commit["sha"][:8]
                msg = commit["commit"]["message"].splitlines()[0][:60]
                date = commit["commit"]["author"]["date"][:10]
                result["commit"] = sha
                result["status"] = f"최신 커밋: {sha} ({date}) {msg}"
            else:
                result["status"] = "커밋 조회 실패"
        except Exception as e:
            result["status"] = f"API 오류: {e}"

    return result


# ── 텍스트 유사도 (Jaccard + TF-IDF) ─────────────────────────────────────────
def tokenize(text):
    text = re.sub(r"[^\w\s가-힣]", " ", (text or "").lower())
    return [w for w in text.split() if w not in STOP_WORDS and len(w) > 1]


def jaccard(a_tokens, b_tokens):
    sa, sb = set(a_tokens), set(b_tokens)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def tfidf_cosine(a_tokens, b_tokens, idf):
    def vec(tokens):
        tf = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        total = len(tokens) or 1
        return {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}

    va, vb = vec(a_tokens), vec(b_tokens)
    keys = set(va) & set(vb)
    if not keys:
        return 0.0
    dot = sum(va[k] * vb[k] for k in keys)
    mag_a = math.sqrt(sum(v ** 2 for v in va.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in vb.values()))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def build_idf(all_tokens_list):
    N = len(all_tokens_list)
    df = defaultdict(int)
    for tokens in all_tokens_list:
        for t in set(tokens):
            df[t] += 1
    return {t: math.log((N + 1) / (cnt + 1)) + 1 for t, cnt in df.items()}


def find_duplicates(real_issues):
    if len(real_issues) < 2:
        return []

    def issue_text(i):
        return (i["title"] or "") + " " + (i.get("body") or "")

    all_tokens = [tokenize(issue_text(i)) for i in real_issues]
    idf = build_idf(all_tokens)

    groups = []
    assigned = set()

    for i in range(len(real_issues)):
        for j in range(i + 1, len(real_issues)):
            a, b = real_issues[i], real_issues[j]
            if a["number"] in assigned and b["number"] in assigned:
                continue
            ta, tb = all_tokens[i], all_tokens[j]
            sim = max(jaccard(ta, tb), tfidf_cosine(ta, tb, idf))
            if sim >= SIMILARITY_THRESHOLD:
                merged = False
                for g in groups:
                    if a["number"] in g or b["number"] in g:
                        g.add(a["number"])
                        g.add(b["number"])
                        merged = True
                        break
                if not merged:
                    groups.append({a["number"], b["number"]})
                assigned.add(a["number"])
                assigned.add(b["number"])

    num_to_issue = {i["number"]: i for i in real_issues}
    return [
        [num_to_issue[n] for n in sorted(g, reverse=True) if n in num_to_issue]
        for g in groups
    ]


# ── 해결된 이슈 감지 ─────────────────────────────────────────────────────────
def is_resolved(issue):
    if issue["state"] == "closed":
        return False
    labels = {l["name"].lower() for l in issue.get("labels", [])}
    if labels & AUTO_CLOSE_LABELS:
        return True
    text = (issue["title"] or "") + " " + (issue.get("body") or "")
    return bool(RESOLVED_RE.search(text))


def is_stale(issue):
    if issue["state"] == "closed":
        return False
    updated = datetime.fromisoformat(issue["updated_at"].replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - updated
    return delta.days >= STALE_DAYS


# ── 보고서 이슈 판별 ─────────────────────────────────────────────────────────
def is_bot_report(issue):
    if BOT_TITLE_RE.search(issue.get("title", "")):
        return True
    body = issue.get("body") or ""
    return bool(ANY_MARKER_RE.search(body))


# ── 통합 보고서 이슈 생성 ─────────────────────────────────────────────────────
def build_report_body(git_info, dup_groups, resolved_closed, stale_closed, open_issues, prev_closed):
    git_section = f"""## 🔄 Git 동기화

| 항목 | 내용 |
|------|------|
| 저장소 | `{REPO_OWNER}/{REPO_NAME}` |
| 브랜치 | `{git_info['branch']}` |
| 모드 | {git_info['mode']} |
| 상태 | {git_info['status'].strip().replace(chr(10), ' / ') or '정상'} |
"""

    if dup_groups:
        dup_section = f"## 🔗 중복 이슈 그룹 ({len(dup_groups)}건)\n\n"
        for idx, members in enumerate(dup_groups, 1):
            dup_section += f"### 그룹 {idx}\n"
            for m in members:
                status_icon = "🟢" if m["state"] == "open" else "⚫"
                dup_section += f"- {status_icon} #{m['number']} {m['title']} (`{m['state']}`)\n"
            dup_section += "\n"
    else:
        dup_section = "## 🔗 중복 이슈 그룹\n\n중복 없음\n\n"

    auto_section = ""
    if resolved_closed or stale_closed:
        total = len(resolved_closed) + len(stale_closed)
        auto_section = f"## ✅ 자동 종결 ({total}건)\n\n"
        for i in resolved_closed:
            auto_section += f"- ✅ #{i['number']} {i['title']} — 해결 완료 감지\n"
        for i in stale_closed:
            auto_section += f"- 💤 #{i['number']} {i['title']} — {STALE_DAYS}일 이상 업데이트 없음\n"
        auto_section += "\n"
    else:
        auto_section = "## ✅ 자동 종결\n\n처리 없음\n\n"

    if open_issues:
        open_section = f"## 📌 열린 이슈 ({len(open_issues)}건)\n\n"
        open_section += "| # | 제목 | 레이블 | 담당자 | 작성자 |\n"
        open_section += "|---|------|--------|--------|--------|\n"
        for i in open_issues:
            labels_str = " ".join(f"`{l['name']}`" for l in i.get("labels", [])) or "없음"
            assignees = ", ".join(f"@{a['login']}" for a in i.get("assignees", [])) or "미지정"
            author = f"@{i['user']['login']}"
            created = i["created_at"][:10]
            open_section += f"| #{i['number']} | {i['title']} | {labels_str} | {assignees} | {author} ({created}) |\n"
        open_section += "\n"
    else:
        open_section = "## 📌 열린 이슈\n\n없음\n\n"

    prev_note = ""
    if prev_closed:
        prev_note = f"\n> 이전 보고서 #{', #'.join(str(n) for n in prev_closed[:5])}"
        if len(prev_closed) > 5:
            prev_note += f" 외 {len(prev_closed)-5}건"
        prev_note += " 종결 처리됨\n"

    return f"""{REPORT_MARKER}
# 📋 이슈 관리 통합 보고서

**업데이트:** {now_str}
**에이전트:** Claude Code (claude-sonnet-4-6) v10
**모드:** GitHub API

---

{git_section}
---

{dup_section}
---

{auto_section}
---

{open_section}
---
{prev_note}
*이 보고서는 이슈 관리 에이전트가 자동 생성했습니다.*
"""


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  KnowledgeBuilder Issue Manager v10")
    print(f"  실행: {now_str}")
    if DRY_RUN:
        print("  [DRY RUN 모드 — 실제 변경 없음]")
    print("=" * 55)

    # 레이블 준비
    setup_labels()

    # 1. Git sync
    print("\n[1/6] Git 동기화...")
    git_info = git_sync()
    print(f"  모드: {git_info['mode']}")
    print(f"  상태: {git_info['status'].strip()[:120]}")

    # 2. 이슈 로드
    print("\n[2/6] 이슈 로드...")
    all_issues = get_all_issues("all")
    real_issues = [i for i in all_issues if not is_bot_report(i)]
    bot_reports = [i for i in all_issues if is_bot_report(i)]
    open_real = [i for i in real_issues if i["state"] == "open"]
    print(f"  실제 이슈: {len(real_issues)}건 (open: {len(open_real)}건)")
    print(f"  봇 보고서: {len(bot_reports)}건")

    # 3. 중복 탐지
    print("\n[3/6] 중복 이슈 탐지...")
    dup_groups = find_duplicates(real_issues)
    print(f"  중복 그룹: {len(dup_groups)}개 발견")

    dup_notified = []
    for members in dup_groups:
        nums_str = ", ".join(f"#{m['number']}" for m in members)
        print(f"  그룹: {nums_str}")
        open_members = [m for m in members if m["state"] == "open"]
        if len(open_members) > 1:
            canonical = open_members[0]  # 번호 큰 게 최신
            for dupe in open_members[1:]:
                add_labels(dupe["number"], ["duplicate"])
                comment = (
                    f"@{dupe['user']['login']}\n\n"
                    f"이 이슈는 #{canonical['number']} **{canonical['title']}** 와 중복으로 감지되었습니다.\n\n"
                    f"> 그룹 구성원: {nums_str}\n\n"
                    f"#{canonical['number']} 에서 계속 논의해 주세요. "
                    f"잘못된 판정이라면 `duplicate` 레이블을 제거하고 댓글로 알려주세요."
                )
                post_comment(dupe["number"], comment)
                dup_notified.append(dupe["number"])

    # 4. 해결/stale 이슈 종결
    print("\n[4/6] 이슈 자동 종결...")
    resolved_closed, stale_closed = [], []
    for issue in open_real:
        mentions = set()
        mentions.add("@" + issue["user"]["login"])
        for a in issue.get("assignees", []):
            mentions.add("@" + a["login"])
        mention_str = " ".join(sorted(mentions))

        if is_resolved(issue):
            comment = (
                f"{mention_str}\n\n"
                f"이 이슈는 내용에 따라 **해결 완료**로 판단되어 자동 종결합니다.\n\n"
                f"이슈가 실제로 미해결 상태라면 언제든지 다시 열어주세요."
            )
            if close_issue_api(issue["number"]):
                post_comment(issue["number"], comment)
                resolved_closed.append(issue)
                print(f"  종결(해결): #{issue['number']} {issue['title']}")

        elif is_stale(issue):
            comment = (
                f"{mention_str}\n\n"
                f"이 이슈는 **{STALE_DAYS}일 이상 업데이트가 없어** stale로 자동 종결합니다.\n\n"
                f"여전히 유효한 이슈라면 다시 열어 진행 상황을 업데이트해 주세요."
            )
            if close_issue_api(issue["number"], reason="not_planned"):
                post_comment(issue["number"], comment)
                stale_closed.append(issue)
                print(f"  종결(stale): #{issue['number']} {issue['title']}")

    print(f"  해결 종결: {len(resolved_closed)}건 | stale 종결: {len(stale_closed)}건")

    # 5. 남은 오픈 이슈 알림
    print("\n[5/6] 열린 이슈 담당자/작성자 알림...")
    closed_set = {i["number"] for i in resolved_closed + stale_closed}
    remaining_open = [i for i in open_real if i["number"] not in closed_set]
    notified = []
    for issue in remaining_open:
        mentions = set()
        mentions.add("@" + issue["user"]["login"])
        for a in issue.get("assignees", []):
            mentions.add("@" + a["login"])
        mention_str = " ".join(sorted(mentions))
        labels_str = ", ".join(f"`{l['name']}`" for l in issue.get("labels", [])) or "없음"

        comment = (
            f"{mention_str}\n\n"
            f"**[이슈 관리 에이전트] 정기 현황 알림** — {now_str}\n\n"
            f"| 항목 | 내용 |\n"
            f"|------|------|\n"
            f"| 이슈 | #{issue['number']} |\n"
            f"| 레이블 | {labels_str} |\n"
            f"| 생성일 | {issue['created_at'][:10]} |\n\n"
            f"진행 상황을 업데이트하거나 담당자를 지정해 주세요."
        )
        if post_comment(issue["number"], comment):
            notified.append(issue["number"])
            print(f"  알림: #{issue['number']} → {mention_str}")

    # 6. 보고서 생성
    print("\n[6/6] 보고서 업데이트...")

    # 이전 열린 보고서 모두 Close
    open_reports = [r for r in bot_reports if r["state"] == "open"]
    prev_closed = []
    for r in open_reports:
        if close_issue_api(r["number"], reason="not_planned"):
            prev_closed.append(r["number"])
    if prev_closed:
        print(f"  이전 보고서 종결: #{', #'.join(str(n) for n in prev_closed)}")

    # 새 보고서 생성
    report_body = build_report_body(
        git_info, dup_groups, resolved_closed, stale_closed, remaining_open, prev_closed
    )
    report_title = f"[이슈관리] 통합 정리 보고서 (최신: {now_str})"
    new_report = create_issue(report_title, report_body, ["bot", "issue-management"])
    print(f"  새 보고서: #{new_report.get('number', '?')} {new_report.get('html_url', '')}")

    # ── 요약 출력 ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  실행 결과 요약")
    print("=" * 55)
    print(f"  Git 모드:       {git_info['mode']}")
    print(f"  실제 이슈:      {len(real_issues)}건 (open {len(open_real)}건)")
    print(f"  중복 그룹:      {len(dup_groups)}개")
    print(f"  자동 종결:      {len(resolved_closed) + len(stale_closed)}건")
    print(f"  알림 발송:      {len(notified)}건 — {notified}")
    print(f"  이전 보고서:    {len(prev_closed)}건 종결")
    print(f"  새 보고서:      #{new_report.get('number', '?')}")
    print(f"  URL: {new_report.get('html_url', '')}")
    print("=" * 55)

    return {
        "report_number": new_report.get("number"),
        "report_url": new_report.get("html_url"),
        "dup_groups": len(dup_groups),
        "resolved_closed": len(resolved_closed),
        "stale_closed": len(stale_closed),
        "notified": notified,
        "open_issues": len(remaining_open),
    }


if __name__ == "__main__":
    result = main()
    print("\n결과:", json.dumps(result, ensure_ascii=False, indent=2))
