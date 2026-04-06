전체 검증 파이프라인 실행.

아래 순서로 실행하고 결과를 요약해주세요:

1. **verify.py**: `cd /mnt/c/Users/USER/Desktop/크림봇 && PYTHONPATH=. python3 scripts/verify.py`
2. **pytest**: `cd /mnt/c/Users/USER/Desktop/크림봇 && python3 -m pytest tests/test_kream_realtime.py tests/test_matcher.py -v`
3. **문법 검증**: 최근 수정된 .py 파일을 `python3 -c "import ast; ast.parse(open('<파일>').read())"` 로 검증

각 단계에서:
- 성공 시 다음 단계로 진행
- 실패 시 원인 분석 후 자동 수정 시도 (최대 3회)
- 3회 실패 시 멈추고 상세 에러 보고

최종 결과를 표로 요약:
| 단계 | 결과 | 비고 |
