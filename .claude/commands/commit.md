커밋 + 푸시 자동화 (안전 모드).

## 절대 규칙
- **`git add .` 절대 금지** — untracked DB(67MB)·.env·쿠키·probe 스크립트 사고 차단
- 항상 **변경 파일을 명시적으로 add** (화이트리스트 방식)
- **DB 파일(*.db, *.db-shm, *.db-wal) 발견 시 즉시 중단**하고 사용자에게 보고

## 절차

1. **`git status`로 변경 내역 확인** (untracked 포함 전부 출력)

2. **위험 파일 사전 차단**:
   - DB 파일이 변경/untracked 목록에 있으면 → 중단, 사용자에게 .gitignore 누수 보고
   - `.env`, `*credentials*`, `*token*`, `*.key`, `*.pem` 발견 시 → 중단
   - 50MB 이상 파일 → 중단, 사용자 확인 받기

3. **변경 분석 + 한국어 커밋 메시지 생성** (feat/fix/docs/refactor/perf/test/chore 접두사)

4. **명시적 화이트리스트 add** — 변경된 파일을 카테고리별로 묶어 add:
   - 코드: `git add src/ main.py`
   - 테스트: `git add tests/`
   - 문서: `git add docs/ CLAUDE.md HANDOFF.md README.md`
   - 설정: `git add .claude/ pyproject.toml .gitignore`
   - **`git add .` / `git add -A` / `git add -u` 사용 금지**
   - untracked 신규 파일은 사용자에게 묻고 진행

5. **테스트 통과 확인**:
   `python3 -m pytest tests/test_kream_realtime.py tests/test_matcher.py -x -q`
   - 실패 시 커밋 중단, 원인 보고

6. **커밋**: `git commit -m "<생성된 메시지>"`

7. **푸시 전 한 번 더 확인**:
   - `git diff --stat HEAD~1 HEAD` 출력해서 사용자에게 보여주기
   - 50MB 이상 파일 push 위험 재확인

8. **푸시**: `git push`

9. 결과 요약 출력 (커밋 해시, 변경 파일 수, 푸시된 브랜치)

## 사고 발생 시
실수로 DB나 .env 가 커밋된 경우 — push 전이면 `git reset HEAD~1` 후 재커밋. push 후면 `git filter-repo` 또는 BFG 필요 + 토큰/비밀번호 즉시 회전.
