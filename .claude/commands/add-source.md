새 소싱처 추가 원스텝 자동화 — URL 하나로 분석 → 구현 → 테스트 → 커밋까지.

사용법: `/add-source <URL>`
예시: `/add-source https://hoka.com/ko`

## 실행 순서

### 1단계: 소싱처 분석
`source-analyzer` 에이전트를 디스패치하여 종합 분석.

```
Agent(subagent_type="source-analyzer", prompt="<URL> 소싱처 분석. 카탈로그 덤프 가능 여부, 재고 정확성, 매칭 방식, 최적 기법 판별.")
```

분석 결과 판정:
- **GO**: 2단계 진행
- **HARD**: 사용자에게 보고 후 판단 요청
- **DROP**: 사유 보고 후 중단

### 2단계: 크롤러 구현
`crawler-builder` 에이전트를 디스패치. 분석 결과의 최적 기법으로 구현.

```
Agent(subagent_type="crawler-builder", prompt="<분석 결과 포함> 크롤러 구현. 기법: <httpx/curl_cffi/Playwright>")
```

구현 산출물:
- `src/crawlers/{name}.py` — 크롤러 본체 (최적 기법 적용)
- `tests/test_{name}.py` — 단위 테스트
- 카탈로그 덤프 가능 시 `dump_catalog()` 메서드 포함

### 3단계: 검증
```bash
pytest tests/test_{name}.py -v          # 신규 테스트
pytest tests/ -v                         # 전체 회귀
PYTHONPATH=. python scripts/verify.py    # 파이프라인 검증
```

### 4단계: 레지스트리 등록
`src/crawlers/registry.py`에 등록 + 서킷브레이커 + Rate Limit 설정

### 5단계: CLAUDE.md 업데이트
- 소싱처 목록에 추가 (기법 명시)
- Rate Limit 테이블 추가
- 서킷브레이커 목록 추가

### 6단계: 커밋
```bash
git add src/crawlers/{name}.py tests/test_{name}.py CLAUDE.md
git commit -m "feat: {소싱처명} 소싱처 추가 — {기법} {스캔방식}"
```

### 7단계: 실서버 검증
`live-tester` 에이전트로 end-to-end 테스트:
```
Agent(subagent_type="live-tester", prompt="{name} 소싱처 실서버 검증")
```

## 판정 기준

| 단계 | 성공 조건 | 실패 시 |
|------|----------|--------|
| 분석 | 4항목 판별 완료 | DROP 보고 |
| 구현 | 코드 + 테스트 생성 | 에러 보고 |
| 테스트 | 전체 통과 | 수정 후 재시도 (3회) |

## 안전 규칙

- 읽기 전용 원칙 준수
- 기존 크롤러 파일 수정 금지
- 테스트 전체 통과 전 커밋 금지
- 3회 실패 시 사용자에게 보고 후 중단
