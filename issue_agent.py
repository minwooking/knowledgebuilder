#!/usr/bin/env python3
"""
GitHub Issue Management Agent
- git fetch & pull origin master
- 중복 이슈 탐지 및 그룹화
- 종결된 이슈 자동 close
- 담당자/작성자에게 코멘트로 알림
"""

import os
import subprocess
import json
import re
import math
from collections import defaultdict
from typing import Optional
import urllib.request
import urllib.error
import urllib.parse

# ── 설정 ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_OWNER   = "minwooking"
REPO_NAME    = "knowledgebuilder"
REPO_PATH    = "/data/workspace/knowledgebuilder"
BASE_BRANCH  = "master"          # 또는 "main"
API_BASE     = "https://api.github.com"

# 유사도 임계값 (0~1): 이 값 이상이면 중복으로 판단
SIMILARITY_THRESHOLD = 0.55

# 종결 판단 키워드 (이슈 제목/본문 포함 여부)
CLOSED_KEYWORDS = [
    "완료", "done", "완결", "해결", "resolved", "fixed", "close", "closed",
    "finish", "finished", "적용완료", "구현완료", "배포완료",
]

# ── GitHub API 헬퍼 ─────────────────────────────────────────────────────────

def gh_request(method: str, path: str, body: Optional[dict] = None) -> dict | list:
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
            return json.loads(resp.read().decode())
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
        issues.extend(batch)
        page += 1
    return issues


def post_comment(issue_number: int, body: str):
    gh_request(
        "POST",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
        {"body": body},
    )


def close_issue(issue_number: int):
    gh_request(
        "PATCH",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}",
        {"state": "closed", "state_reason": "completed"},
    )


def add_label(issue_number: int, label: str):
    gh_request(
        "POST",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels",
        {"labels": [label]},
    )


def ensure_label(name: str, color: str, description: str = ""):
    existing = gh_request("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels?per_page=100")
    if isinstance(existing, list):
        if any(l.get("name") == name for l in existing):
            return
    gh_request(
        "POST",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
        {"name": name, "color": color, "description": description},
    )


# ── TF-IDF 유사도 ────────────────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    text = re.sub(r"[^\w\s가-힣]", " ", text.lower())
    return [t for t in text.split() if len(t) > 1]


def tfidf_vectors(docs: list[list[str]]) -> list[dict]:
    df: dict[str, int] = defaultdict(int)
    N = len(docs)
    for doc in docs:
        for term in set(doc):
            df[term] += 1

    vectors = []
    for doc in docs:
        tf: dict[str, float] = defaultdict(float)
        for term in doc:
            tf[term] += 1
        total = max(len(doc), 1)
        vec = {
            term: (count / total) * math.log((N + 1) / (df[term] + 1) + 1)
            for term, count in tf.items()
        }
        vectors.append(vec)
    return vectors


def cosine_sim(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v**2 for v in a.values()))
    norm_b = math.sqrt(sum(v**2 for v in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


# ── Git 작업 ────────────────────────────────────────────────────────────────

def run_git(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", REPO_PATH] + args,
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def git_fetch_pull():
    print("\n[1] git fetch + pull")
    # fetch
    rc, out = run_git(["fetch", "--all"])
    print(f"  fetch → {out or 'ok'}")

    # pull origin master (또는 main)
    for branch in [BASE_BRANCH, "main", "master"]:
        rc, out = run_git(["pull", "origin", branch])
        if rc == 0:
            print(f"  pull origin {branch} → {out[:120] or 'ok'}")
            break
        if "couldn't find remote ref" not in out:
            print(f"  pull origin {branch} → {out[:120]}")


# ── 핵심 로직 ────────────────────────────────────────────────────────────────

def find_duplicates(issues: list) -> list[list[int]]:
    """유사도 기반 중복 이슈 그룹 반환 (각 그룹 = [issue_number, ...])"""
    texts = [
        f"{i['title']} {i.get('body') or ''}" for i in issues
    ]
    tokens = [tokenize(t) for t in texts]
    vectors = tfidf_vectors(tokens)

    n = len(issues)
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
            if cosine_sim(vectors[i], vectors[j]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(issues[idx]["number"])

    return [g for g in groups.values() if len(g) > 1]


def is_likely_done(issue: dict) -> bool:
    text = f"{issue['title']} {issue.get('body') or ''}".lower()
    if any(kw in text for kw in CLOSED_KEYWORDS):
        return True
    labels = [l["name"].lower() for l in issue.get("labels", [])]
    return any(kw in lbl for kw in ["done", "complete", "wontfix", "resolved"] for lbl in labels)


def mention(issue: dict) -> str:
    people = {issue["user"]["login"]}
    for a in issue.get("assignees", []):
        people.add(a["login"])
    return " ".join(f"@{p}" for p in sorted(people))


# ── 메인 실행 ────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN 환경변수가 없습니다.")
        return

    # ① git fetch & pull
    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        git_fetch_pull()
    else:
        print(f"\n[1] {REPO_PATH}는 git 저장소가 아닙니다 — git 단계 건너뜀")

    # ② 라벨 준비
    print("\n[2] 라벨 준비")
    ensure_label("duplicate", "cfd3d5", "중복 이슈")
    ensure_label("wontfix",   "ffffff", "종결/불필요")
    print("  done")

    # ③ 열린 이슈 수집
    print("\n[3] 열린 이슈 수집")
    issues = get_all_issues("open")
    print(f"  총 {len(issues)}개")
    if not issues:
        print("  처리할 이슈 없음 — 종료")
        return

    # ④ 중복 탐지
    print("\n[4] 중복 이슈 탐지")
    dup_groups = find_duplicates(issues)
    issue_map = {i["number"]: i for i in issues}

    closed_in_this_run: set[int] = set()

    for group in dup_groups:
        # 가장 먼저 만들어진 이슈(숫자 낮은 것)를 원본으로
        group_sorted = sorted(group)
        original_num = group_sorted[0]
        dups = group_sorted[1:]

        original = issue_map.get(original_num)
        if not original:
            continue

        group_titles = ", ".join(
            f"#{n} {issue_map[n]['title']!r}" for n in group_sorted if n in issue_map
        )
        print(f"  중복 그룹: {group_titles}")

        for dup_num in dups:
            dup = issue_map.get(dup_num)
            if not dup:
                continue
            add_label(dup_num, "duplicate")
            people = mention(dup)
            msg = (
                f"## :twisted_rightwards_arrows: 중복 이슈 알림\n\n"
                f"{people}\n\n"
                f"이 이슈는 #{original_num} **{original['title']}** 과(와) 중복으로 "
                f"판단되어 `duplicate` 라벨이 추가되었습니다.\n\n"
                f"- 원본 이슈: #{original_num}\n"
                f"- 자동 분석 일시: {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"> 이 이슈는 원본 이슈 해결 시 함께 닫힐 예정입니다."
            )
            post_comment(dup_num, msg)
            close_issue(dup_num)
            closed_in_this_run.add(dup_num)
            print(f"    #{dup_num} → 중복 라벨 + close + 알림")

        # 원본에도 그룹 요약 코멘트
        dup_list = "\n".join(f"- #{n}" for n in dups)
        orig_msg = (
            f"## :mag: 중복 이슈 묶음 요약\n\n"
            f"{mention(original)}\n\n"
            f"아래 이슈들이 이 이슈의 중복으로 탐지되어 닫혔습니다:\n{dup_list}"
        )
        post_comment(original_num, orig_msg)

    # ⑤ 종결 이슈 탐지 및 close
    print("\n[5] 종결 이슈 자동 close")
    for issue in issues:
        num = issue["number"]
        if num in closed_in_this_run:
            continue
        if is_likely_done(issue):
            people = mention(issue)
            msg = (
                f"## :white_check_mark: 이슈 자동 종결 알림\n\n"
                f"{people}\n\n"
                f"제목/내용/라벨 분석 결과 이 이슈가 **완료**된 것으로 판단되어 자동으로 닫습니다.\n\n"
                f"- 이슈: #{num} **{issue['title']}**\n"
                f"- 종결 근거: 완료 키워드 또는 라벨 감지\n"
                f"- 처리 일시: {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"> 잘못 닫힌 경우 이슈를 다시 열고 **wontfix** 라벨을 제거해 주세요."
            )
            post_comment(num, msg)
            close_issue(num)
            closed_in_this_run.add(num)
            print(f"  #{num} '{issue['title']}' → 종결 처리")

    # ⑥ 최종 요약
    total_remaining = len(issues) - len(closed_in_this_run)
    print(
        f"\n[요약] 총 {len(issues)}개 이슈 / "
        f"중복 그룹 {len(dup_groups)}개 / "
        f"이번 run 종결 {len(closed_in_this_run)}개 / "
        f"잔여 open {total_remaining}개"
    )
    print("\n완료.")


if __name__ == "__main__":
    main()
