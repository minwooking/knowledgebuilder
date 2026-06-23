# knowledgebuilder — GitHub Issue Manager Agent

GitHub 이슈를 자동으로 정리하는 에이전트:

- **git fetch + pull** origin master/main 자동 동기화
- **중복 이슈 감지** — TF-IDF 코사인 유사도로 중복 이슈 그룹화 후 close
- **종결 이슈 자동 close** — 완료 키워드 / stale(장기 미활동) 이슈 처리
- **담당자·작성자 알림** — 각 이슈에 @mention 댓글 게시
- **보고서 이슈 자동 생성** — 전체 처리 결과를 새 이슈로 요약

## 빠른 시작

```bash
pip install requests scikit-learn
export GITHUB_REPO=owner/your-repo
DRY_RUN=true bash agents/run_issue_manager.sh   # dry-run 먼저
bash agents/run_issue_manager.sh                  # 실제 실행
```

## 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `REPO_PATH` | 로컬 리포 경로 | `/data/workspace/knowledgebuilder` |
| `GITHUB_REPO` | `owner/repo` 형식 | git remote에서 자동 감지 |
| `GITHUB_TOKEN` | GitHub Personal Access Token | 환경에서 주입 |
| `DUPLICATE_THRESHOLD` | 중복 감지 임계값 (0~1) | `0.75` |
| `STALE_DAYS` | stale 이슈 기준 일수 | `30` |
| `DRY_RUN` | true이면 실제 변경 없이 로그만 | `false` |
