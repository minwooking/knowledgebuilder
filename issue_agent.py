#!/usr/bin/env python3
"""
GitHub Issue Management Agent v5
- git 최신 커밋 정보를 GitHub API로 조회 (로컬 git 실패 시 API fallback)
- 중복 이슈 감지 및 그룹화 (TF-IDF 코사인 유사도)
- 종결된 이슈 자동 close
- 담당자/작성자에게 이슈 댓글로 @멘션 알림
- 봇 보고서 이슈 중복 생성 방지 (마커 통합)
- 이전 중복 봇 보고서 자동 close 정리
"""

import os
import sys
import json
import re
import math
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

# ── 설정 ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
REPO_OWNER        = os.environ.get("REPO_OWNER", "minwooking")
REPO_NAME         = os.environ.get("REPO_NAME", "knowledgebuilder")
REPO_PATH         = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
API_BASE          = "https://api.github.com"
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
SIMILARITY_THRESH = float(os.environ.get("SIMILARITY_THRESHOLD", "0.55"))
STALE_DAYS        = int(os.environ.get("STALE_DAYS", "30"))

BOT_LABELS = {"bot", "issue-management", "automated"}

# 봇 보고서 이슈 제목 패턴
BOT_REPORT_TITLE_RE = re.compile(
    r"^\[(이슈관리|이슈 관리|자동보고)\]", re.IGNORECASE
)

# 마스터 보고서 마커 (이전 버전 포함)
REPORT_MARKERS = [
    "<!-- issue-manager-report -->",
    "<!-- kb-issue-manager-final -->",
    "<!-- kb-issue-manager -->",
    "<!-- issue-manager-v",
]

MASTER_MARKER = "<!-- issue-manager-report -->"  # v5 표준

RESOLVED_RE = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|"
    r"완료|해결|닫|종결|수정완료|구현완료|배포완료|적용완료)\b",
    re.IGNORECASE,
)


# ── GitHub API ───────────────────────────────────────────────────────────────

def gh(method: str, path: str, body: Optional[dict] = None,
       params: Optional[dict] = None) -> dict | list:
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


def get_all_issues(state: str = "open") -> list:
    results, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                   params={"state": state, "per_page": 100, "page": page})
        if not isinstance(batch, list) or not batch:
            break
        results.extend(i for i in batch if "pull_request" not in i)
        page += 1
    return results


def post_comment(num: int, body: str):
    if DRY_RUN:
        print(f"  [DRY] comment #{num}: {body[:100]}...")
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/comments", {"body": body})


def close_issue(num: int, reason: str = "completed"):
    if DRY_RUN:
        print(f"  [DRY] close #{num}")
        return
    gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
       {"state": "closed", "state_reason": reason})


def add_labels(num: int, labels: list):
    if DRY_RUN:
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/labels", {"labels": labels})


def ensure_label(name: str, color: str, desc: str = ""):
    existing = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                  params={"per_page": 100})
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
           {"name": name, "color": color, "description": desc})


# ── Git 동기화 (로컬 우선, 실패 시 API fallback) ─────────────────────────────

def run_git(args: list) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", REPO_PATH] + args,
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_sync_api() -> dict:
    """GitHub API로 최신 브랜치 정보 조회."""
    report = {"fetch": "API", "pull": "", "branch": "", "errors": [], "mode": "api"}
    try:
        repo_info = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
        branch = repo_info.get("default_branch", "main")
        report["branch"] = branch

        commits = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/commits",
                     params={"sha": branch, "per_page": 3})
        if isinstance(commits, list) and commits:
            latest = commits[0]
            sha = latest["sha"][:8]
            msg = latest["commit"]["message"].split("\n")[0][:80]
            date = latest["commit"]["author"]["date"][:10]
            report["pull"] = f"최신 커밋: {sha} ({date}) {msg}"
            print(f"  ✓ API 브랜치: {branch} | {sha} {msg}")
        else:
            report["errors"].append("커밋 조회 실패")
    except Exception as e:
        report["errors"].append(f"API 오류: {e}")
    return report


def git_sync() -> dict:
    """로컬 git 시도 후 실패 시 API fallback."""
    report = {"fetch": "", "pull": "", "branch": "", "errors": [], "mode": "local"}

    if not os.path.isdir(os.path.join(REPO_PATH, ".git")):
        print(f"  ⚠ 로컬 저장소 없음 ({REPO_PATH}), GitHub API로 대체")
        return git_sync_api()

    print("  git fetch --prune origin ...")
    rc, out = run_git(["fetch", "--prune", "origin"])
    report["fetch"] = out[:300]
    if rc != 0:
        report["errors"].append(f"fetch 실패: {out[:200]}")
        print(f"  ⚠ fetch 실패, API fallback")
        return git_sync_api()

    _, remote_head = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"])
    branch = "master" if "master" in remote_head else "main"

    print(f"  git pull origin {branch} ...")
    rc, out = run_git(["pull", "origin", branch])
    report["pull"] = out[:300]
    report["branch"] = branch
    if rc != 0:
        alt = "master" if branch == "main" else "main"
        rc2, out2 = run_git(["pull", "origin", alt])
        report["pull"] = out2[:300]
        report["branch"] = alt
        if rc2 != 0:
            report["errors"].append(f"pull 실패: {out2[:200]}")

    return report


# ── 봇 이슈 판별 ─────────────────────────────────────────────────────────────

def is_bot_issue(issue: dict) -> bool:
    labels = {l["name"].lower() for l in issue.get("labels", [])}
    if labels & BOT_LABELS:
        return True
    return bool(BOT_REPORT_TITLE_RE.match(issue.get("title", "")))


def has_report_marker(issue: dict) -> bool:
    body = issue.get("body") or ""
    return any(marker in body for marker in REPORT_MARKERS)


# ── TF-IDF 유사도 ────────────────────────────────────────────────────────────

def tokenize(text: str) -> list:
    text = re.sub(r"[^\w\s가-힣a-zA-Z0-9]", " ", text.lower())
    return [t for t in text.split() if len(t) > 1]


def cosine_sim(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v**2 for v in a.values()))
    nb = math.sqrt(sum(v**2 for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def make_tfidf_vectors(docs: list) -> list:
    N = len(docs)
    df: dict[str, int] = defaultdict(int)
    tokens_list = [tokenize(d) for d in docs]
    for toks in tokens_list:
        for t in set(toks):
            df[t] += 1
    vecs = []
    for toks in tokens_list:
        tf: dict[str, float] = defaultdict(float)
        for t in toks:
            tf[t] += 1
        total = max(len(toks), 1)
        vec = {t: (c / total) * math.log((N + 1) / (df[t] + 1) + 1)
               for t, c in tf.items()}
        vecs.append(vec)
    return vecs


def find_duplicate_groups(issues: list, threshold: float) -> list:
    n = len(issues)
    if n < 2:
        return []

    texts = [f"{i['title']} {(i.get('body') or '')}".strip() for i in issues]
    vecs = make_tfidf_vectors(texts)

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if cosine_sim(vecs[i], vecs[j]) >= threshold:
                union(i, j)

    groups: dict[int, list] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    result = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        group = sorted([issues[k] for k in idxs], key=lambda x: x["number"])
        result.append(group)
    return result


# ── 종결 판단 ────────────────────────────────────────────────────────────────

def check_resolved(issue: dict) -> Optional[str]:
    if is_bot_issue(issue):
        return None

    labels = [l["name"].lower() for l in issue.get("labels", [])]
    close_labels = {"resolved", "wontfix", "won't fix", "duplicate", "invalid", "done"}
    matched = [l for l in labels if any(kw in l for kw in close_labels)]
    if matched:
        return f"라벨: {matched}"

    title = issue.get("title", "")
    body = (issue.get("body") or "")
    if RESOLVED_RE.search(title) or RESOLVED_RE.search(body):
        return "제목/본문에 완료 키워드 감지"

    updated_at = issue.get("updated_at", "")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - updated
            if delta.days >= STALE_DAYS:
                return f"{delta.days}일 이상 미활동 (stale)"
        except ValueError:
            pass
    return None


# ── 알림 헬퍼 ────────────────────────────────────────────────────────────────

def get_mentions(issue: dict) -> str:
    people = {issue["user"]["login"]}
    for a in issue.get("assignees") or []:
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))


def notify_duplicate_group(group: list):
    keeper = group[0]
    dups = group[1:]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dup_list = "\n".join(f"- #{d['number']} {d['title']}" for d in dups)

    # 대표 이슈에 통합 알림
    keeper_msg = (
        f"## 🔗 중복 이슈 통합 알림\n\n"
        f"{get_mentions(keeper)}\n\n"
        f"아래 이슈들이 이 이슈와 유사한 내용으로 **중복** 감지되어 통합 처리됩니다:\n\n"
        f"{dup_list}\n\n"
        f"중복 이슈는 닫히며, 이 이슈(#{keeper['number']})에서 계속 논의해 주세요.\n\n"
        f"> 🤖 자동 분석 · {now_str}"
    )
    post_comment(keeper["number"], keeper_msg)

    for dup in dups:
        dup_msg = (
            f"## 🚫 중복 이슈 종결\n\n"
            f"{get_mentions(dup)}\n\n"
            f"이 이슈는 **#{keeper['number']} {keeper['title']}** 와(과) 중복으로 판정되어 자동으로 닫힙니다.\n\n"
            f"- 대표 이슈: #{keeper['number']} → {keeper.get('html_url','')}\n"
            f"- 처리 일시: {now_str}\n\n"
            f"잘못 닫힌 경우 이슈를 다시 열어 주세요.\n\n"
            f"> 🤖 자동 처리 · {now_str}"
        )
        post_comment(dup["number"], dup_msg)
        add_labels(dup["number"], ["duplicate"])
        close_issue(dup["number"], "not_planned")
        print(f"    #{dup['number']} 중복 → close + @{dup['user']['login']} 알림")


def notify_auto_close(issue: dict, reason: str):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"## ✅ 이슈 자동 종결\n\n"
        f"{get_mentions(issue)}\n\n"
        f"이 이슈는 해결된 것으로 판단되어 자동 종결됩니다.\n\n"
        f"- 종결 사유: {reason}\n"
        f"- 처리 일시: {now_str}\n\n"
        f"재논의가 필요하면 이슈를 다시 열어 주세요.\n\n"
        f"> 🤖 자동 처리 · {now_str}"
    )
    post_comment(issue["number"], msg)
    close_issue(issue["number"], "completed")
    print(f"    #{issue['number']} 종결 → close + @{issue['user']['login']} 알림")


# ── 이전 봇 보고서 정리 ─────────────────────────────────────────────────────

def cleanup_old_bot_reports(all_open: list, master_num: Optional[int] = None) -> int:
    """마스터 보고서 외 open 봇 보고서를 모두 close."""
    count = 0
    for issue in all_open:
        if issue["number"] == master_num:
            continue
        if (is_bot_issue(issue) or has_report_marker(issue)):
            if not DRY_RUN:
                close_issue(issue["number"], "not_planned")
            print(f"    #{issue['number']} 이전 봇 보고서 → close")
            count += 1
    return count


# ── 마스터 보고서 upsert ─────────────────────────────────────────────────────

def upsert_report(git_report: dict, dup_groups: list, auto_closed: list,
                  remaining_open: list, cleaned_up: int) -> Optional[int]:
    from datetime import timedelta as _td
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    kst_now = datetime.now(timezone(_td(hours=9)))
    now_kst = kst_now.strftime("%Y-%m-%d %H:%M KST")

    # ── 본문 작성 ──
    lines = [
        MASTER_MARKER,
        "# 📋 이슈 관리 자동 보고서",
        "",
        f"**업데이트:** {now_kst} ({now_str})",
        "",
    ]

    # Git 동기화 섹션
    lines += ["## 🔄 Git 동기화", ""]
    mode = git_report.get("mode", "local")
    branch = git_report.get("branch", "?")
    if git_report.get("errors"):
        for e in git_report["errors"]:
            lines.append(f"- ⚠️ {e}")
    else:
        pull_out = git_report.get("pull", "")
        status = "✅ 최신 상태"
        if "Already up to date" not in pull_out and "up to date" not in pull_out.lower() and mode == "local":
            status = "⬆️ 업데이트됨"
        lines.append(f"- 브랜치: `{branch}` | 모드: `{mode}`")
        lines.append(f"- 상태: {status}")
        if pull_out:
            lines.append(f"- 내역: `{pull_out[:120]}`")

    # 중복 그룹 섹션
    lines += ["", f"## 🔗 중복 이슈 그룹 ({len(dup_groups)}건)", ""]
    if dup_groups:
        for group in dup_groups:
            keeper = group[0]
            dups = group[1:]
            lines.append(
                f"- **대표** #{keeper['number']} *{keeper['title']}*"
                + " ← 중복: " + ", ".join(f"#{d['number']}" for d in dups)
            )
    else:
        lines.append("- 중복 없음")

    # 자동 종결 섹션
    lines += ["", f"## ✅ 자동 종결 ({len(auto_closed)}건)", ""]
    if auto_closed:
        for issue, reason in auto_closed:
            lines.append(f"- #{issue['number']} *{issue['title']}* — {reason}")
    else:
        lines.append("- 없음")

    # 정리 섹션
    if cleaned_up > 0:
        lines += ["", f"## 🧹 이전 봇 보고서 정리 ({cleaned_up}건)", ""]
        lines.append(f"- 중복 봇 보고서 이슈 {cleaned_up}건 close 처리됨")

    # 잔여 이슈 섹션
    real_open = [i for i in remaining_open if not is_bot_issue(i)]
    lines += ["", f"## 📌 처리 필요 이슈 ({len(real_open)}건)", ""]
    if real_open:
        for issue in real_open:
            assignees = [a["login"] for a in issue.get("assignees") or []]
            assign_str = " → 담당: " + ", ".join(f"@{a}" for a in assignees) if assignees else " → ⚠️ 담당자 미지정"
            labels_str = " `" + "` `".join(l["name"] for l in issue.get("labels", [])) + "`" if issue.get("labels") else ""
            lines.append(f"- #{issue['number']} *{issue['title']}*{labels_str}{assign_str}")
    else:
        lines.append("- 없음 🎉")

    lines += [
        "",
        "---",
        f"> 🤖 이슈 관리 에이전트 v5 · {now_kst}",
        "> 이 이슈는 자동으로 업데이트됩니다.",
    ]
    body = "\n".join(lines)
    title = f"[이슈관리] 통합 정리 보고서 {kst_now.strftime('%Y-%m-%d')} (v5)"

    if DRY_RUN:
        print(f"  [DRY] 보고서:\n{body[:500]}...")
        return None

    # 기존 마스터 보고서 탐색
    all_open = get_all_issues("open")
    existing = None
    for issue in all_open:
        if has_report_marker(issue) and is_bot_issue(issue):
            existing = issue
            break

    if existing:
        gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{existing['number']}",
           {"title": title, "body": body})
        print(f"  보고서 #{existing['number']} 업데이트 완료 → {title}")
        return existing["number"]
    else:
        result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                    {"title": title, "body": body, "labels": ["bot", "issue-management"]})
        num = result.get("number")
        print(f"  보고서 #{num} 신규 생성 완료 → {title}")
        return num


# ── 잔여 이슈 알림 ──────────────────────────────────────────────────────────

def notify_remaining(remaining: list, report_num: Optional[int]):
    """처리 필요한 이슈 작성자/담당자에게 알림 댓글."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_link = (f" | [관리 보고서 #{report_num}](https://github.com/"
                   f"{REPO_OWNER}/{REPO_NAME}/issues/{report_num})" if report_num else "")

    for issue in remaining:
        if is_bot_issue(issue):
            continue
        assignees = [a["login"] for a in issue.get("assignees") or []]
        if not assignees:
            # 담당자 없으면 작성자에게 알림
            msg = (
                f"## 📬 이슈 현황 알림\n\n"
                f"@{issue['user']['login']}\n\n"
                f"이 이슈가 아직 처리 중입니다. 담당자 지정 및 진행 상황을 업데이트해 주세요.\n\n"
                f"- 현재 상태: `open`\n"
                f"- 라벨: {', '.join('`'+l['name']+'`' for l in issue.get('labels',[])) or '없음'}\n"
                f"- 업데이트: {now_str}{report_link}\n\n"
                f"> 🤖 자동 알림 · {now_str}"
            )
        else:
            mention_str = " ".join(f"@{a}" for a in assignees)
            msg = (
                f"## 📬 이슈 현황 알림\n\n"
                f"{mention_str} (담당자) @{issue['user']['login']} (작성자)\n\n"
                f"이 이슈의 진행 상황을 업데이트해 주세요.\n\n"
                f"- 현재 상태: `open`\n"
                f"- 라벨: {', '.join('`'+l['name']+'`' for l in issue.get('labels',[])) or '없음'}\n"
                f"- 업데이트: {now_str}{report_link}\n\n"
                f"> 🤖 자동 알림 · {now_str}"
            )
        post_comment(issue["number"], msg)
        print(f"    #{issue['number']} → @{issue['user']['login']} 잔여 이슈 알림 발송")


# ── 메인 ────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 또는 GH_TOKEN 환경변수가 필요합니다.")
        sys.exit(1)

    print("=" * 62)
    print("  GitHub Issue Management Agent v5")
    print(f"  대상: {REPO_OWNER}/{REPO_NAME} | DRY_RUN={DRY_RUN}")
    print(f"  중복 임계값: {SIMILARITY_THRESH} | Stale: {STALE_DAYS}일")
    print("=" * 62)

    # 1. Git 동기화
    print("\n[1/6] Git 동기화")
    git_report = git_sync()
    if git_report.get("errors"):
        print(f"  ⚠ 오류: {git_report['errors']}")

    # 2. 이슈 수집
    print("\n[2/6] 이슈 수집")
    ensure_label("duplicate", "cfd3d5", "중복 이슈")
    ensure_label("bot", "0075ca", "봇 자동 생성")
    ensure_label("issue-management", "e4e669", "이슈 관리 자동화")
    all_open = get_all_issues("open")
    real_issues = [i for i in all_open if not is_bot_issue(i)]
    bot_reports = [i for i in all_open if is_bot_issue(i) or has_report_marker(i)]
    print(f"  전체 open: {len(all_open)}개 | 실제 이슈: {len(real_issues)}개 "
          f"| 봇 보고서: {len(bot_reports)}개")

    # 3. 중복 감지
    print(f"\n[3/6] 중복 감지 (임계값={SIMILARITY_THRESH})")
    dup_groups = find_duplicate_groups(real_issues, SIMILARITY_THRESH)
    dup_numbers = {i["number"] for g in dup_groups for i in g[1:]}
    print(f"  중복 그룹: {len(dup_groups)}개, 중복 이슈: {len(dup_numbers)}건")

    for group in dup_groups:
        keeper = group[0]
        dups = group[1:]
        print(f"  그룹 #{keeper['number']} '{keeper['title'][:50]}' "
              f"← {', '.join('#'+str(d['number']) for d in dups)}")
        notify_duplicate_group(group)

    # 4. 종결 이슈 처리
    print("\n[4/6] 종결 이슈 처리")
    auto_closed = []
    for issue in real_issues:
        if issue["number"] in dup_numbers:
            continue
        reason = check_resolved(issue)
        if reason:
            print(f"  #{issue['number']} '{issue['title'][:50]}' → {reason}")
            notify_auto_close(issue, reason)
            auto_closed.append((issue, reason))

    if not auto_closed:
        print("  종결 대상 없음")

    # 5. 보고서 upsert + 이전 봇 보고서 정리
    print("\n[5/6] 관리 보고서 업데이트 + 이전 보고서 정리")
    updated_open = get_all_issues("open")
    report_num = upsert_report(
        git_report, dup_groups, auto_closed, updated_open, 0
    )

    # 방금 생성/업데이트한 보고서 외 open 봇 보고서 정리
    updated_open2 = get_all_issues("open")
    cleaned = cleanup_old_bot_reports(updated_open2, master_num=report_num)
    if cleaned > 0:
        print(f"  이전 봇 보고서 {cleaned}건 정리 완료")
        # 보고서 본문 업데이트 (정리 건수 반영)
        final_open = get_all_issues("open")
        upsert_report(git_report, dup_groups, auto_closed, final_open, cleaned)

    # 6. 잔여 이슈 알림
    print("\n[6/6] 잔여 이슈 담당자/작성자 알림")
    final_open = get_all_issues("open")
    remaining_real = [i for i in final_open if not is_bot_issue(i)]
    if remaining_real:
        notify_remaining(remaining_real, report_num)
    else:
        print("  처리 필요한 이슈 없음")

    # 결과 요약
    total_closed = len(dup_numbers) + len(auto_closed)
    remaining_count = len(remaining_real)

    print("\n" + "=" * 62)
    print(f"  완료 요약")
    print(f"  - Git 브랜치: {git_report.get('branch','?')} (모드: {git_report.get('mode','?')})")
    print(f"  - 중복 그룹: {len(dup_groups)}개")
    print(f"  - 자동 종결: {total_closed}개")
    print(f"  - 봇 보고서 정리: {cleaned}건")
    print(f"  - 잔여 처리 이슈: {remaining_count}개")
    print(f"  - 보고서 이슈: #{report_num}")
    print("=" * 62)

    return {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "git_branch": git_report.get("branch"),
        "git_mode": git_report.get("mode"),
        "git_errors": git_report.get("errors", []),
        "duplicate_groups": len(dup_groups),
        "auto_closed": total_closed,
        "bot_reports_cleaned": cleaned,
        "remaining_open": remaining_count,
        "report_issue": report_num,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
