#!/usr/bin/env python3
"""
GitHub Issue Management Agent v4
- git fetch & pull (master/main 자동 감지)
- 중복 이슈 감지 및 그룹화 (TF-IDF 코사인 유사도)
- 종결된 이슈 자동 close
- 담당자/작성자에게 이슈 댓글로 알림
- 관리 보고서를 upsert (중복 생성 방지)
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
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── 설정 ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
REPO_OWNER        = os.environ.get("REPO_OWNER", "minwooking")
REPO_NAME         = os.environ.get("REPO_NAME", "knowledgebuilder")
REPO_PATH         = os.environ.get("REPO_PATH", "/data/workspace/knowledgebuilder")
API_BASE          = "https://api.github.com"
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
SIMILARITY_THRESH = float(os.environ.get("SIMILARITY_THRESHOLD", "0.60"))
STALE_DAYS        = int(os.environ.get("STALE_DAYS", "30"))

# 봇/자동화 이슈는 중복 감지 및 종결 대상에서 제외
BOT_LABELS = {"bot", "issue-management", "automated"}

# 종결 판단 키워드 (제목/본문)
RESOLVED_RE = re.compile(
    r"\b(fix(?:ed|es)?|resolve[sd]?|close[sd]?|done|completed?|완료|해결|닫|종결|수정완료|구현완료|배포완료|적용완료)\b",
    re.IGNORECASE,
)


# ── GitHub API ───────────────────────────────────────────────────────────────

def gh(method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None):
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


def get_issues(state: str = "open") -> list:
    results, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                   params={"state": state, "per_page": 100, "page": page})
        if not isinstance(batch, list) or not batch:
            break
        # PR 제외
        results.extend(i for i in batch if "pull_request" not in i)
        page += 1
    return results


def post_comment(num: int, body: str):
    if DRY_RUN:
        print(f"  [DRY] comment #{num}: {body[:80]}...")
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/comments", {"body": body})


def close_issue(num: int):
    if DRY_RUN:
        print(f"  [DRY] close #{num}")
        return
    gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}",
       {"state": "closed", "state_reason": "completed"})


def add_labels(num: int, labels: list):
    if DRY_RUN:
        return
    gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}/labels", {"labels": labels})


def ensure_label(name: str, color: str, desc: str = ""):
    existing = gh("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels", params={"per_page": 100})
    if isinstance(existing, list) and any(l.get("name") == name for l in existing):
        return
    if not DRY_RUN:
        gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
           {"name": name, "color": color, "description": desc})


# ── Git ──────────────────────────────────────────────────────────────────────

def run_git(args: list) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", REPO_PATH] + args,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def git_sync() -> dict:
    report = {"fetch": "", "pull": "", "branch": "", "errors": []}
    if not os.path.isdir(os.path.join(REPO_PATH, ".git")):
        msg = f"git 저장소 없음: {REPO_PATH}"
        report["errors"].append(msg)
        print(f"  ⚠ {msg}")
        return report

    print("  git fetch --prune origin ...")
    rc, out = run_git(["fetch", "--prune", "origin"])
    report["fetch"] = out[:300]
    if rc != 0:
        report["errors"].append(f"fetch 실패: {out[:200]}")

    # 기본 브랜치 자동 감지
    _, remote_head = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"])
    branch = "main"
    if "master" in remote_head:
        branch = "master"

    print(f"  git pull origin {branch} ...")
    rc, out = run_git(["pull", "origin", branch])
    report["pull"] = out[:300]
    report["branch"] = branch
    if rc != 0:
        # 반대 브랜치 재시도
        alt = "master" if branch == "main" else "main"
        print(f"  재시도: git pull origin {alt} ...")
        rc2, out2 = run_git(["pull", "origin", alt])
        report["pull"] = out2[:300]
        report["branch"] = alt
        if rc2 != 0:
            report["errors"].append(f"pull 실패: {out2[:200]}")
    return report


# ── TF-IDF 유사도 ────────────────────────────────────────────────────────────

def tokenize(text: str) -> list:
    text = re.sub(r"[^\w\s가-힣a-zA-Z]", " ", text.lower())
    return [t for t in text.split() if len(t) > 1]


def cosine_sim_sparse(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v**2 for v in a.values()))
    nb = math.sqrt(sum(v**2 for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def make_vectors(docs: list) -> list:
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
    """Union-Find로 중복 그룹 반환. 각 그룹 = [대표이슈, 중복1, ...]"""
    n = len(issues)
    if n < 2:
        return []

    texts = [f"{i['title']} {i.get('body') or ''}".strip() for i in issues]
    vecs = make_vectors(texts)

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
            if cosine_sim_sparse(vecs[i], vecs[j]) >= threshold:
                union(i, j)

    groups: dict[int, list] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    result = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        # 번호 낮은 이슈가 대표
        group = sorted([issues[k] for k in idxs], key=lambda x: x["number"])
        result.append(group)
    return result


# ── 종결 판단 ────────────────────────────────────────────────────────────────

def is_bot_issue(issue: dict) -> bool:
    labels = {l["name"].lower() for l in issue.get("labels", [])}
    return bool(labels & BOT_LABELS)


def check_resolved(issue: dict) -> Optional[str]:
    """종결 사유 반환 (없으면 None)."""
    if is_bot_issue(issue):
        return None

    labels = [l["name"].lower() for l in issue.get("labels", [])]
    label_keywords = {"resolved", "wontfix", "won't fix", "duplicate", "invalid", "done"}
    if any(kw in lbl for kw in label_keywords for lbl in labels):
        return f"라벨 감지: {labels}"

    title = issue.get("title", "").lower()
    body = (issue.get("body") or "").lower()
    if RESOLVED_RE.search(title) or RESOLVED_RE.search(body):
        return "제목/본문에 완료 키워드 포함"

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


# ── 알림 메시지 ──────────────────────────────────────────────────────────────

def mentions(issue: dict) -> str:
    people = {issue["user"]["login"]}
    for a in issue.get("assignees") or []:
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))


def notify_duplicate_group(group: list):
    keeper = group[0]
    dups = group[1:]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dup_lines = "\n".join(f"- #{d['number']} {d['title']}" for d in dups)

    # 대표 이슈 코멘트
    keeper_msg = (
        f"## 🔗 중복 이슈 묶음 알림\n\n"
        f"{mentions(keeper)}\n\n"
        f"다음 이슈들이 이 이슈와 유사한 내용으로 감지되어 중복 처리됩니다:\n\n"
        f"{dup_lines}\n\n"
        f"중복 이슈는 이 이슈에 통합하여 진행 권장합니다.\n\n"
        f"> 자동 분석 일시: {now_str}"
    )
    post_comment(keeper["number"], keeper_msg)

    # 중복 이슈 각각 알림 + close
    for dup in dups:
        dup_msg = (
            f"## 🚫 중복 이슈 종결 알림\n\n"
            f"{mentions(dup)}\n\n"
            f"이 이슈는 #{keeper['number']} **{keeper['title']}** 와(과) 유사한 내용으로 "
            f"중복 판정되어 자동으로 닫힙니다.\n\n"
            f"- 대표 이슈: #{keeper['number']} — {keeper.get('html_url','')}\n"
            f"- 처리 일시: {now_str}\n\n"
            f"> 잘못 닫힌 경우 이슈를 다시 열어 주세요."
        )
        post_comment(dup["number"], dup_msg)
        add_labels(dup["number"], ["duplicate"])
        close_issue(dup["number"])
        print(f"    #{dup['number']} 중복 → close + 알림")


def notify_auto_close(issue: dict, reason: str):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"## ✅ 이슈 자동 종결 알림\n\n"
        f"{mentions(issue)}\n\n"
        f"- 이슈: #{issue['number']} **{issue['title']}**\n"
        f"- 종결 사유: {reason}\n"
        f"- 처리 일시: {now_str}\n\n"
        f"이슈가 해결된 것으로 판단되어 자동 종결합니다. "
        f"재논의가 필요하면 이슈를 다시 열어 주세요."
    )
    post_comment(issue["number"], msg)
    close_issue(issue["number"])
    print(f"    #{issue['number']} 종결 → close + 알림")


# ── 관리 보고서 upsert ───────────────────────────────────────────────────────

REPORT_LABEL  = "issue-management"
REPORT_MARKER = "<!-- issue-manager-report -->"


def upsert_summary_report(git_report: dict, dup_groups: list, auto_closed: list,
                           remaining_open: list):
    """기존 관리 보고서 이슈를 찾아 업데이트, 없으면 새로 생성."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 보고서 본문 생성
    lines = [
        REPORT_MARKER,
        f"# 📋 이슈 관리 자동 보고서",
        f"",
        f"**업데이트:** {now_str}",
        f"",
    ]

    # Git 동기화
    lines += ["## 🔄 Git 동기화", ""]
    if git_report.get("errors"):
        for e in git_report["errors"]:
            lines.append(f"- ⚠️ {e}")
    else:
        lines.append(f"- 브랜치: `{git_report.get('branch','?')}`")
        pull_out = git_report.get('pull', '')
        lines.append(f"- 상태: {'✅ 최신' if 'Already up to date' in pull_out or 'up to date' in pull_out.lower() else '⬆️ 업데이트됨'}")

    # 중복 그룹
    lines += ["", f"## 🔗 중복 이슈 그룹 ({len(dup_groups)}건)", ""]
    if dup_groups:
        for group in dup_groups:
            keeper = group[0]
            dups = group[1:]
            lines.append(
                f"- **대표** #{keeper['number']} *{keeper['title']}* ← "
                + ", ".join(f"#{d['number']}" for d in dups) + " (중복 close됨)"
            )
    else:
        lines.append("- 중복 없음")

    # 자동 종결
    lines += ["", f"## ✅ 자동 종결 이슈 ({len(auto_closed)}건)", ""]
    if auto_closed:
        for issue, reason in auto_closed:
            lines.append(f"- #{issue['number']} *{issue['title']}* — {reason}")
    else:
        lines.append("- 없음")

    # 잔여 열린 이슈
    real_open = [i for i in remaining_open if not is_bot_issue(i)]
    lines += ["", f"## 📌 잔여 열린 이슈 ({len(real_open)}건)", ""]
    if real_open:
        for issue in real_open:
            assignees = [a["login"] for a in issue.get("assignees") or []]
            assign_str = f" → 담당: {', '.join('@'+a for a in assignees)}" if assignees else ""
            lines.append(f"- #{issue['number']} *{issue['title']}*{assign_str}")
    else:
        lines.append("- 없음")

    lines += ["", "---", "> 이 이슈는 자동 이슈 관리 에이전트(v4)가 관리합니다."]
    body = "\n".join(lines)
    title = f"[이슈관리] 보고서 {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (통합 정리)"

    if DRY_RUN:
        print(f"  [DRY] 보고서 upsert:\n{body[:400]}")
        return

    # 기존 보고서 이슈 탐색 (open 상태 + REPORT_MARKER 포함)
    all_open = get_issues("open")
    existing = None
    for issue in all_open:
        if REPORT_MARKER in (issue.get("body") or "") and is_bot_issue(issue):
            existing = issue
            break

    if existing:
        # 업데이트
        gh("PATCH", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{existing['number']}",
           {"title": title, "body": body})
        print(f"  보고서 이슈 #{existing['number']} 업데이트 완료")
    else:
        # 신규 생성
        result = gh("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                    {"title": title, "body": body, "labels": ["bot", "issue-management"]})
        num = result.get("number", "?")
        print(f"  보고서 이슈 #{num} 신규 생성 완료")


# ── 메인 ────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN 또는 GH_TOKEN 환경변수가 필요합니다.")
        sys.exit(1)

    print("=" * 60)
    print("GitHub Issue Manager Agent v4")
    print(f"대상: {REPO_OWNER}/{REPO_NAME} | DRY_RUN={DRY_RUN}")
    print(f"중복 임계값: {SIMILARITY_THRESH} | Stale: {STALE_DAYS}일")
    print("=" * 60)

    # 1. Git 동기화
    print("\n[1/5] Git 동기화")
    git_report = git_sync()
    if git_report["errors"]:
        print(f"  ⚠ 오류: {git_report['errors']}")
    else:
        print(f"  ✓ 완료 (브랜치: {git_report['branch']})")

    # 2. 이슈 수집
    print("\n[2/5] 이슈 수집")
    ensure_label("duplicate", "cfd3d5", "중복 이슈")
    all_open = get_issues("open")
    # 봇 이슈 제외한 실제 이슈만
    real_issues = [i for i in all_open if not is_bot_issue(i)]
    print(f"  전체 open: {len(all_open)}개 | 실제 이슈: {len(real_issues)}개")

    # 3. 중복 감지
    print(f"\n[3/5] 중복 감지 (임계값={SIMILARITY_THRESH})")
    dup_groups = find_duplicate_groups(real_issues, SIMILARITY_THRESH)
    print(f"  중복 그룹: {len(dup_groups)}개")

    dup_numbers = {i["number"] for g in dup_groups for i in g[1:]}

    for group in dup_groups:
        keeper = group[0]
        dups = group[1:]
        print(f"  그룹: #{keeper['number']} '{keeper['title']}' ← "
              + ", ".join(f"#{d['number']}" for d in dups))
        notify_duplicate_group(group)

    # 4. 종결 이슈 처리
    print("\n[4/5] 종결 이슈 처리")
    auto_closed = []
    for issue in real_issues:
        if issue["number"] in dup_numbers:
            continue
        reason = check_resolved(issue)
        if reason:
            print(f"  #{issue['number']} '{issue['title']}' → {reason}")
            notify_auto_close(issue, reason)
            auto_closed.append((issue, reason))

    if not auto_closed:
        print("  종결 대상 없음")

    # 5. 보고서 upsert
    print("\n[5/5] 관리 보고서 업데이트")
    # 처리 후 최신 open 이슈 다시 조회
    updated_open = get_issues("open")
    upsert_summary_report(git_report, dup_groups, auto_closed, updated_open)

    # 결과 요약
    closed_count = len(dup_numbers) + len(auto_closed)
    remaining = len([i for i in updated_open if not is_bot_issue(i)])
    print("\n" + "=" * 60)
    print(f"완료: 중복 그룹 {len(dup_groups)}개 / 자동 종결 {closed_count}개 / 잔여 {remaining}개")
    print("=" * 60)

    return {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "git_branch": git_report.get("branch"),
        "git_errors": git_report.get("errors", []),
        "duplicate_groups": len(dup_groups),
        "auto_closed": closed_count,
        "remaining_open": remaining,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
