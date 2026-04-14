커밋 + 푸시 자동화.

1. `git status`와 `git diff --stat`으로 변경 내역 확인
2. 변경 내용을 분석하여 한국어 커밋 메시지 자동 생성 (feat/fix/docs/refactor 접두사)
3. `git add .` 실행
4. `git commit -m "<생성된 메시지>"` 실행
5. `git push` 실행
6. 결과 요약 출력

주의:
- .env, credentials, 비밀키 파일은 절대 커밋하지 마세요
- 커밋 전 `python3 -m pytest tests/test_kream_realtime.py tests/test_matcher.py -x -q`로 테스트 통과 확인
- 테스트 실패 시 커밋하지 말고 실패 원인을 보고하세요
