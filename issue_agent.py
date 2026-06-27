#!/usr/bin/env python3
"""
GitHub Issue Management Agent v8
- git fetch & pull origin master (로컬 우선, 실패 시 API fallback)
- 중복 이슈 감지 (TF-IDF 코사인 유사도) + 그룹 정리
- 종결된 이슈 자동 close (라벨/키워드/stale 기준)
- 담당자(assignee) + 작성자에게 GitHub 댓글 @멘션 알림
- 마스터 보고서 이슈 단일 유지 (이전 봇 보고서 정리)
"""

import os, sys, json, re, math, subprocess, urllib.request, urllib.error, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── 설정 ──────────────────────────────────────────────────────────────────────
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
REPO_OWNER        = os.environ.get("REPO_OWNER", "minwooking")
REPO_NAME         = os.environ.get("REPO_NAME", "knowledgebuilder")
REPO_PATH         = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
API_BASE          = "https://api.github.com"
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
SIMILARITY_THRESH = float(os.environ.get("SIMILARITY_THRESHOLD", "0.55"))
STALE_DAYS        = int(os.environ.get("STALE_DAYS", "30"))

# 봇 보고서 마커 (모든 버전 포함)
BOT_REPORT_TITLE_RE = re.compile(r"^\[(이슈관리|이슈 관리|자동보고)\]", re.IGNORECASE)
REPORT_MARKERS = [
    "<!-- issue-manager-report -->",
    "<!-- kb-issue-manager-final -->",
    "<!-- kb-issue-manager -->",
    "<!-- issue-manager-v",  # v1~v999 모두 매칭
]
MASTER_MARKER = "<!-- issue-manager-v8 -->"
BOT_LABELS = {"bot", "issue-management", "automated"}

RESOLVED_RE = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|"
    r"완료|해결|닫|종결|수정완료|구현완료|배포완료|적용완료)\b",
    re.IGNORECASE,
)

KST = timezone(timedelta(hours=9))


# ── GitHub API ────────────────────────────────────────────────────────────────

def gh(method: str, path: str, body: Optional[dict] = None,
       params: Optional[dict] = None):
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
        print(f"  [DRY] comment #{num}: {body[:80]}...")
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


def ensure_labels():
    existing = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                  params={"per_page": 100})
    existing_names = {l["name"] for l in existing} if isinstance(existing, list) else set()
    needed = [
        ("duplicate",         "cfd3d5", "중복 이슈"),
        ("bot",               "0075ca", "봇 자동 생성"),
        ("issue-management",  "e4e669", "이슈 관리 자동화"),
        ("needs-assignee",    "fbca04", "담당자 미지정"),
    ]
    for name, color, desc in needed:
        if name not in existing_names and not DRY_RUN:
            gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
               {"name": name, "color": color, "description": desc})


# ── Git 동기화 ────────────────────────────────────────────────────────────────

def run_git(args: list) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", REPO_PATH] + args,
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_sync() -> dict:
    report = {"fetch": "", "pull": "", "branch": "main", "errors": [], "mode": "api"}

    # 로컬 저장소 확인
    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        print("  로컬 git 저장소 감지, fetch/pull 시도...")
        rc, out = run_git(["fetch", "--prune", "origin"])
        report["fetch"] = out[:300]
        if rc == 0:
            # 브랜치 확인
            _, head = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"])
            branch = "master" if "master" in head else "main"
            rc2, out2 = run_git(["pull", "origin", branch])
            if rc2 != 0:
                alt = "master" if branch == "main" else "main"
                rc2, out2 = run_git(["pull", "origin", alt])
                branch = alt if rc2 == 0 else branch
            report["pull"] = out2[:300]
            report["branch"] = branch
            report["mode"] = "local"
            print(f"  ✓ 로컬 git 완료 ({branch})")
            return report
        else:
            report["errors"].append(f"fetch 실패: {out[:150]}")
            print(f"  ⚠ 로컬 fetch 실패, API fallback")
    else:
        print(f"  ℹ 로컬 저장소 없음 ({REPO_PATH}), GitHub API 사용")

    # API fallback
    try:
        repo_info = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
        branch = repo_info.get("default_branch", "main")
        report["branch"] = branch

        commits = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/commits",
                     params={"sha": branch, "per_page": 3})
        if isinstance(commits, list) and commits:
            c = commits[0]
            sha = c["sha"][:8]
            msg = c["commit"]["message"].split("\n")[0][:80]
            date = c["commit"]["author"]["date"][:10]
            report["pull"] = f"최신 커밋: {sha} ({date}) {msg}"
            print(f"  ✓ API 조회 완료: {branch} | {sha} {msg[:50]}")
        else:
            report["errors"].append("커밋 조회 실패")
    except Exception as e:
        report["errors"].append(f"API 오류: {e}")

    return report


# ── 봇 이슈 판별 ──────────────────────────────────────────────────────────────

def is_bot_issue(issue: dict) -> bool:
    labels = {l["name"].lower() for l in issue.get("labels", [])}
    if labels & BOT_LABELS:
        return True
    return bool(BOT_REPORT_TITLE_RE.match(issue.get("title", "")))


def has_report_marker(issue: dict) -> bool:
    body = issue.get("body") or ""
    return any(marker in body for marker in REPORT_MARKERS)


# ── TF-IDF 유사도 ─────────────────────────────────────────────────────────────

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


def make_tfidf(docs: list) -> list:
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
    if len(issues) < 2:
        return []
    texts = [f"{i['title']} {(i.get('body') or '')}".strip() for i in issues]
    vecs = make_tfidf(texts)
    parent = list(range(len(issues)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(len(issues)):
        for j in range(i + 1, len(issues)):
            if cosine_sim(vecs[i], vecs[j]) >= threshold:
                union(i, j)

    groups: dict[int, list] = defaultdict(list)
    for idx in range(len(issues)):
        groups[find(idx)].append(idx)

    result = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        group = sorted([issues[k] for k in idxs], key=lambda x: x["number"])
        result.append(group)
    return result


# ── 종결 판단 ─────────────────────────────────────────────────────────────────

def check_resolved(issue: dict) -> Optional[str]:
    if is_bot_issue(issue):
        return None
    labels = [l["name"].lower() for l in issue.get("labels", [])]
    close_kw = {"resolved", "wontfix", "won't fix", "duplicate", "invalid", "done"}
    matched = [l for l in labels if any(kw in l for kw in close_kw)]
    if matched:
        return f"라벨: {matched}"
    title = issue.get("title", "")
    body = issue.get("body") or ""
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


# ── 알림 ─────────────────────────────────────────────────────────────────────

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

    # 대표 이슈 알림
    post_comment(
        keeper["number"],
        f"## 🔗 중복 이슈 통합\n\n{get_mentions(keeper)}\n\n"
        f"아래 이슈들이 이 이슈와 중복으로 감지되어 통합됩니다:\n\n{dup_list}\n\n"
        f"중복 이슈는 닫히며, 이 이슈(#{keeper['number']})에서 계속 논의해 주세요.\n\n"
        f"> 🤖 자동 분석 · {now_str}"
    )
    # 중복 이슈 각각 알림 + close
    for dup in dups:
        post_comment(
            dup["number"],
            f"## 🚫 중복 이슈 종결\n\n{get_mentions(dup)}\n\n"
            f"이 이슈는 **#{keeper['number']} {keeper['title']}** 와(과) 중복으로 자동 종결됩니다.\n\n"
            f"- 대표 이슈: #{keeper['number']} → {keeper.get('html_url','')}\n"
            f"- 처리 일시: {now_str}\n\n"
            f"잘못 닫힌 경우 이슈를 다시 열어 주세요.\n\n"
            f"> 🤖 자동 처리 · {now_str}"
        )
        add_labels(dup["number"], ["duplicate"])
        close_issue(dup["number"], "not_planned")
        print(f"    dup #{dup['number']} → close, @{dup['user']['login']} 알림")


def notify_auto_close(issue: dict, reason: str):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    post_comment(
        issue["number"],
        f"## ✅ 이슈 자동 종결\n\n{get_mentions(issue)}\n\n"
        f"이 이슈는 해결된 것으로 판단되어 자동 종결됩니다.\n\n"
        f"- 종결 사유: {reason}\n"
        f"- 처리 일시: {now_str}\n\n"
        f"재논의가 필요하면 이슈를 다시 열어 주세요.\n\n"
        f"> 🤖 자동 처리 · {now_str}"
    )
    close_issue(issue["number"], "completed")
    print(f"    #{issue['number']} → close, @{issue['user']['login']} 종결 알림")


def notify_remaining(remaining: list, report_num: Optional[int]):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_link = (
        f" | [관리 보고서 #{report_num}](https://github.com/{REPO_OWNER}/{REPO_NAME}/issues/{report_num})"
        if report_num else ""
    )
    for issue in remaining:
        if is_bot_issue(issue):
            continue
        assignees = [a["login"] for a in issue.get("assignees") or []]
        labels_str = ", ".join(f"`{l['name']}`" for l in issue.get("labels", [])) or "없음"

        if assignees:
            mentions = " ".join(f"@{a}" for a in assignees) + f" @{issue['user']['login']}"
            msg = (
                f"## 📬 이슈 현황 알림\n\n{mentions}\n\n"
                f"이 이슈의 진행 상황을 업데이트해 주세요.\n\n"
                f"- 상태: `open`\n- 라벨: {labels_str}\n- 업데이트: {now_str}{report_link}\n\n"
                f"> 🤖 자동 알림 · {now_str}"
            )
        else:
            msg = (
                f"## 📬 이슈 현황 알림\n\n@{issue['user']['login']}\n\n"
                f"이 이슈가 아직 처리 중입니다. 담당자 지정 및 진행 상황을 업데이트해 주세요.\n\n"
                f"- 상태: `open`\n- 라벨: {labels_str}\n- 업데이트: {now_str}{report_link}\n\n"
                f"> 🤖 자동 알림 · {now_str}"
            )
        post_comment(issue["number"], msg)
        print(f"    #{issue['number']} → @{issue['user']['login']} 알림 발송")


# ── 이전 봇 보고서 정리 ───────────────────────────────────────────────────────

def cleanup_old_bot_reports(all_open: list, master_num: Optional[int]) -> int:
    count = 0
    for issue in all_open:
        if issue["number"] == master_num:
            continue
        if is_bot_issue(issue) or has_report_marker(issue):
            close_issue(issue["number"], "not_planned")
            print(f"    #{issue['number']} 이전 봇 보고서 → close")
            count += 1
    return count


# ── 마스터 보고서 upsert ──────────────────────────────────────────────────────

def upsert_report(git_report: dict, dup_groups: list, auto_closed: list,
                  remaining_open: list, cleaned: int,
                  existing_num: Optional[int] = None) -> Optional[int]:
    now_utc = datetime.now(timezone.utc)
    now_kst = datetime.now(KST)
    now_kst_str = now_kst.strftime("%Y-%m-%d %H:%M KST")
    now_utc_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    branch = git_report.get("branch", "?")
    mode = git_report.get("mode", "api")
    pull_out = git_report.get("pull", "")
    errors = git_report.get("errors", [])

    real_open = [i for i in remaining_open if not is_bot_issue(i)]

    # ── 보고서 본문 ──
    lines = [MASTER_MARKER, "# 📋 이슈 관리 보고서", ""]
    lines += [f"**업데이트:** {now_kst_str}", f"**모드:** `{mode}` | 브랜치: `{branch}`", "---", ""]

    # Git 동기화
    lines += ["## 🔄 Git 동기화", ""]
    if errors:
        lines += [f"- ⚠️ {e}" for e in errors]
    else:
        status = "⬆️ 업데이트됨" if (mode == "local" and "Already up to date" not in pull_out) else "✅ 최신 상태"
        lines.append(f"```\n{pull_out[:200]}\n```")
        lines.append(f"상태: {status}")
    lines += [""]

    # 중복 이슈
    lines += [f"## 🔗 중복 이슈 ({len(dup_groups)}건)", ""]
    if dup_groups:
        for g in dup_groups:
            keeper = g[0]
            dups_str = ", ".join(f"#{d['number']}" for d in g[1:])
            lines.append(f"- **대표** #{keeper['number']} *{keeper['title']}* ← 중복: {dups_str}")
    else:
        lines.append("중복 없음")
    lines += [""]

    # 자동 종결
    lines += [f"## ✅ 자동 종결 ({len(auto_closed)}건)", ""]
    if auto_closed:
        for issue, reason in auto_closed:
            lines.append(f"- #{issue['number']} *{issue['title']}* — {reason}")
    else:
        lines.append("없음")
    lines += [""]

    # 처리 필요 이슈
    lines += [f"## 📌 처리 필요 이슈 ({len(real_open)}건)", ""]
    for issue in real_open:
        assignees = [a["login"] for a in issue.get("assignees") or []]
        assign_str = ", ".join(f"@{a}" for a in assignees) if assignees else "⚠️ 미지정"
        labels_str = " ".join(f"`{l['name']}`" for l in issue.get("labels", []))
        created = issue.get("created_at", "")[:10]
        lines += [
            f"### #{issue['number']} · {issue['title']}",
            f"- **작성자:** @{issue['user']['login']}",
            f"- **담당자:** {assign_str}",
            f"- **레이블:** {labels_str or '없음'}",
            f"- **생성일:** {created}",
            "",
        ]

    # 알림 발송
    notified = [(i, "needs-assignee" if not i.get("assignees") else "status-update")
                for i in real_open]
    lines += [f"## 📨 알림 발송 ({len(notified)}건)", ""]
    if notified:
        lines.append("| 대상 | 이슈 | 유형 |")
        lines.append("|------|------|------|")
        for issue, typ in notified:
            lines.append(f"| @{issue['user']['login']} | #{issue['number']} | {typ} |")
    else:
        lines.append("없음")
    lines += [""]

    if cleaned > 0:
        lines += [f"## 🧹 이전 봇 보고서 정리 ({cleaned}건)", ""]
        lines.append(f"중복 봇 보고서 이슈 {cleaned}건 close 처리됨")
        lines += [""]

    lines += [
        "---",
        f"> 🤖 이슈 관리 에이전트 v8 · Claude Code · {now_kst_str}",
        "> 이 이슈는 에이전트 실행 시마다 자동으로 업데이트됩니다.",
        "",
        "---",
        "_Generated by [Claude Code](https://claude.ai/code)_",
    ]

    body = "\n".join(lines)
    title = f"[이슈관리] 통합 정리 보고서 (최신: {now_kst_str})"

    if DRY_RUN:
        print(f"  [DRY] 보고서 업데이트:\n{body[:400]}...")
        return None

    if existing_num:
        gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{existing_num}",
           {"title": title, "body": body})
        print(f"  ✓ 보고서 #{existing_num} 업데이트 완료")
        return existing_num

    # 기존 마스터 탐색
    all_open = get_all_issues("open")
    for issue in all_open:
        if has_report_marker(issue) and is_bot_issue(issue):
            gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue['number']}",
               {"title": title, "body": body})
            print(f"  ✓ 보고서 #{issue['number']} 업데이트 완료")
            return issue["number"]

    # 신규 생성
    result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                {"title": title, "body": body, "labels": ["bot", "issue-management"]})
    num = result.get("number")
    print(f"  ✓ 보고서 #{num} 신규 생성 완료")
    return num


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 또는 GH_TOKEN 환경변수 필요")
        sys.exit(1)

    print("=" * 64)
    print("  GitHub Issue Management Agent v8")
    print(f"  대상: {REPO_OWNER}/{REPO_NAME} | DRY_RUN={DRY_RUN}")
    print(f"  중복 임계값: {SIMILARITY_THRESH} | Stale: {STALE_DAYS}일")
    print("=" * 64)

    # 1. Git 동기화
    print("\n[1/6] Git 동기화")
    git_report = git_sync()
    if git_report.get("errors"):
        print(f"  ⚠ 경고: {git_report['errors']}")

    # 2. 이슈 수집 + 레이블 보장
    print("\n[2/6] 이슈 수집")
    ensure_labels()
    all_open = get_all_issues("open")
    real_issues = [i for i in all_open if not is_bot_issue(i)]
    bot_reports = [i for i in all_open if is_bot_issue(i) or has_report_marker(i)]
    print(f"  전체 open: {len(all_open)}개 | 실제 이슈: {len(real_issues)}개 | 봇 보고서: {len(bot_reports)}개")

    # 3. 중복 감지
    print(f"\n[3/6] 중복 감지 (임계값={SIMILARITY_THRESH})")
    dup_groups = find_duplicate_groups(real_issues, SIMILARITY_THRESH)
    dup_numbers = {i["number"] for g in dup_groups for i in g[1:]}
    print(f"  중복 그룹: {len(dup_groups)}개, 중복 이슈: {len(dup_numbers)}건")
    for group in dup_groups:
        keeper = group[0]
        dups = group[1:]
        print(f"  그룹 #{keeper['number']} '{keeper['title'][:40]}' ← {', '.join('#'+str(d['number']) for d in dups)}")
        notify_duplicate_group(group)

    # 4. 종결 이슈 처리
    print("\n[4/6] 종결 이슈 처리")
    auto_closed = []
    for issue in real_issues:
        if issue["number"] in dup_numbers:
            continue
        reason = check_resolved(issue)
        if reason:
            print(f"  #{issue['number']} '{issue['title'][:40]}' → {reason}")
            notify_auto_close(issue, reason)
            auto_closed.append((issue, reason))
    if not auto_closed:
        print("  종결 대상 없음")

    # 5. 마스터 보고서 upsert
    print("\n[5/6] 마스터 보고서 업데이트")
    updated_open = get_all_issues("open")
    report_num = upsert_report(git_report, dup_groups, auto_closed, updated_open, 0)

    # 이전 봇 보고서 정리
    updated_open2 = get_all_issues("open")
    cleaned = cleanup_old_bot_reports(updated_open2, master_num=report_num)
    if cleaned > 0:
        print(f"  이전 봇 보고서 {cleaned}건 정리 완료")
        final_open = get_all_issues("open")
        upsert_report(git_report, dup_groups, auto_closed, final_open, cleaned, existing_num=report_num)

    # 6. 잔여 이슈 담당자/작성자 알림
    print("\n[6/6] 잔여 이슈 알림")
    final_open = get_all_issues("open")
    remaining_real = [i for i in final_open if not is_bot_issue(i)]
    if remaining_real:
        notify_remaining(remaining_real, report_num)
    else:
        print("  처리 필요한 이슈 없음")

    # 요약
    total_closed = len(dup_numbers) + len(auto_closed)
    print("\n" + "=" * 64)
    print("  완료 요약")
    print(f"  - 브랜치: {git_report.get('branch','?')} (모드: {git_report.get('mode','?')})")
    print(f"  - 중복 그룹: {len(dup_groups)}개")
    print(f"  - 자동 종결: {total_closed}개")
    print(f"  - 봇 보고서 정리: {cleaned}건")
    print(f"  - 잔여 처리 이슈: {len(remaining_real)}개")
    print(f"  - 보고서 이슈: #{report_num}")
    print("=" * 64)

    return {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "branch": git_report.get("branch"),
        "mode": git_report.get("mode"),
        "duplicate_groups": len(dup_groups),
        "auto_closed": total_closed,
        "bot_reports_cleaned": cleaned,
        "remaining_open": len(remaining_real),
        "report_issue": report_num,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
