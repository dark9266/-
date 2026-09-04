---
paths:
  - "tests/**/*.py"
  - "scripts/verify*.py"
  - "conftest.py"
---

# 테스트·검증 규칙

## 🔴 테스트 격리 (2026-07-25 실계정 311콜 사고 이후 — 기계가 강제)
- `main()`·배치 진입점을 부르는 테스트는 **tmp DB + 네트워크 mock 필수**.
  `tests/conftest.py` 가 실 송신·운영 DB 연결을 차단하고 `settings.db_path` 를 tmp 로 격리하지만,
  그건 **마지막 방어선**이다. 거기 의존하지 마라.
- **가드에 안전을 의존하지 말 것** — 가드 판별력을 변이(mutation) 검증할 때 그 가드를 끄는 순간이 사고다.
  변이 전에 "이 변이가 실호출 경로를 여는가"를 먼저 묻는다.
- 안전망 해제(`KREAM_TEST_ALLOW_NETWORK` / `KREAM_TEST_ALLOW_REAL_DB`)는 **사장 GO 사안**.

## 버그 수정 원칙
- **3회 실패 시 방법 지정 금지** — 문제 + 제약 + 실패 이력만 제공하고 탐색은 Claude 가 한다.
- **단위 테스트만으로 검증 금지** — 해당 버그를 **재현하는 테스트**가 통과해야 검증이다.
- 검증 실패 시 사람 개입 없이 재수정 → 재검증.
- 버그 신호가 있으면 `superpowers:systematic-debugging` 스킬을 먼저 부른다.
  파이프라인 추적("왜 안 잡혀/알림 안 와")이면 `scan-debugger` 에이전트가 우선.

## 자동 검증 루프 (커밋 전 순서)
1. `PYTHONPATH=. python scripts/verify.py` — 파이프라인 검증
2. `pytest tests/ -v` — 전체 테스트
3. `python3 -c "import ast; ast.parse(open('<파일>').read())"` — 문법 검증
4. **전부 성공했을 때만** git commit

`claim_evidence_guard` 훅이 검증 실행 없는 "완료/성공" 단언을 차단한다.
pytest 는 `N passed` 출력이 있어야 하고, 그 실행이 **변경 이후**여야 한다.

## 회귀 테스트
- `tests/fixtures/false_positives.json` 에 케이스 추가
- `status: "known_bug"` → 수정 후 `"fixed"`

## 검수비 2,500원 기대값 정정 (2026-09-04 완료)
`검수비 2,500원 확정(2026-05-02, 커밋 6f57c14)` 당시 `tests/test_profit_calculator.py` 만
갱신되고 아래 4곳의 기대값이 낡은 채 남아 4개월간 거짓 실패를 냈다. **정정 완료 — 재조사 금지.**
```
tests/test_filters.py::TestFeeBoundary::test_zero_price_fee        총수수료 +2,500
tests/test_pipeline.py::TestPipelineProfitCalculation::…            54,350 → 51,850
tests/test_integration.py::test_tier2_buy_sends_alert               kream 108,000 → 110,600
scripts/verify.py                                                   "검수비 = 0원" → 2,500원
```
코드(`src/config.py` `inspection_fee=2500`)가 실 정산서 기준으로 맞다. **공식 재추정 금지.**
남은 verify.py 실패 1건(`inventory_data=None → isDeleted 폴백`)은 MFS 다중재고 알려진 한계로 별건.
