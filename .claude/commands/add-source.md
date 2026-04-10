새 소싱처 추가 원스텝 자동화 — URL 하나로 API 탐색 → 크롤러 구현 → 테스트 → 커밋까지.

사용법: `/add-source <URL>`
예시: `/add-source https://hoka.com/ko`

## 실행 순서

### 1단계: API 탐색
`api-prober` 에이전트를 디스패치하여 대상 사이트의 GET API를 탐색합니다.

```
Agent(subagent_type="api-prober", prompt="<URL> API 탐색. 검색 API + 상세 API + 사이즈/재고 API 찾기.")
```

탐색 결과 판정:
- **GO**: 2단계 진행
- **HARD**: 사용자에게 보고 후 판단 요청
- **DROP**: 사유 보고 후 중단

### 2단계: 크롤러 구현
`crawler-builder` 에이전트를 디스패치하여 크롤러를 구현합니다.

```
Agent(subagent_type="crawler-builder", prompt="<탐색 결과 요약 포함> 크롤러 구현.")
```

구현 산출물:
- `src/crawlers/{name}.py` — 크롤러 본체
- `tests/test_{name}.py` — 단위 테스트

### 3단계: 검증
```bash
pytest tests/test_{name}.py -v          # 신규 테스트
pytest tests/ -v                         # 전체 회귀
PYTHONPATH=. python scripts/verify.py    # 파이프라인 검증
python3 -c "import ast; ast.parse(open('src/crawlers/{name}.py').read())"  # AST
```

### 4단계: 통합 연결
`src/discord_bot/bot.py`에 import 추가:
```python
import src.crawlers.{name}  # noqa: F401
```

### 5단계: CLAUDE.md 업데이트
- 소싱처 목록에 추가
- Rate Limit 테이블 추가
- 서킷브레이커 목록 추가

### 6단계: 커밋
```bash
git add src/crawlers/{name}.py tests/test_{name}.py src/discord_bot/bot.py CLAUDE.md
git commit -m "feat: {소싱처명} 소싱처 추가 — {API 방식 요약}"
```

### 7단계: 실서버 검증 (선택)
`live-tester` 에이전트로 실서버 end-to-end 테스트:
```
Agent(subagent_type="live-tester", prompt="{name} 소싱처 실서버 검증")
```

## 판정 기준

| 단계 | 성공 조건 | 실패 시 |
|------|----------|--------|
| API 탐색 | GET API 발견 | DROP 보고 |
| 크롤러 구현 | 코드 + 테스트 생성 | 에러 보고 |
| 테스트 | 전체 통과 | 수정 후 재시도 (3회) |
| 검증 | verify.py PASS | 수정 후 재시도 |

## 안전 규칙

- GET 전용 원칙 준수
- 기존 크롤러 파일 수정 금지
- 테스트 전체 통과 전 커밋 금지
- 3회 실패 시 사용자에게 보고 후 중단
