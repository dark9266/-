# CLAUDE.md

크림봇 (Kream Monitor Bot) — 크림 차익거래 자동화 Discord 봇. Python 3.12+, async-first.

## Commands

```bash
pip install -e ".[dev]"          # 설치
python main.py                   # 봇 실행
pytest tests/ -v                 # 전체 테스트
PYTHONPATH=. python scripts/verify.py  # 파이프라인 검증
ruff check src/ tests/           # 린트
ruff format src/ tests/          # 포맷
```

## v2 Architecture

### 핵심 파이프라인

```
크림 DB (47k) → 우선순위 큐 → 소싱처 16곳 병렬 검색 → 모델번호 매칭 → 수익 분석 → Discord 알림
```

### 3티어 스캔 구조

| 티어 | 주기 | 역할 | 모듈 |
|------|------|------|------|
| **Tier 0** | 5분/20건 | 연속 배치 스캔 — 47k 전체 순환 | `src/continuous_scanner.py` |
| **Tier 1** | 30분 | 긴급 스캔 — hot 50건 역방향 + 카테고리 신상품 | `src/tier1_scanner.py` |
| **Tier 2** | 60초 | 실시간 폴링 — watchlist 크림 시세 감시 | `src/tier2_monitor.py` |

### 스캔 방향

- **역방향 (주력 70%)**: 크림 DB → 소싱처 검색. 수요 검증된 상품 소싱처 탐색
- **정방향 (보조 30%)**: 무신사/29CM 카테고리 리스팅 → 크림 DB 매칭. 세일/신상품 포착

### hot/warm/cold 우선순위 큐

| 등급 | 조건 | 재스캔 주기 | 검색 소싱처 | 규모 |
|------|------|-----------|-----------|------|
| **hot** | 거래량 ≥ 5 | 2시간 | 16곳 전부 | ~130건 |
| **warm** | 거래량 3~4 | 8시간 | 무신사+나이키+29CM+카시나+튠+살로몬+아크테릭스+W컨셉+웍스아웃 (9곳) | ~16건 |
| **cold** | 거래량 < 3 | 48시간 | 무신사+나이키+그랜드스테이지+온더스팟 (4곳) | ~37,000건 |

DB 컬럼: `next_scan_at`, `scan_priority` (kream_products 테이블)

## Key Modules

### 스캐너
- `src/reverse_scanner.py` — 역방향 스캐너. 크림 hot → 소싱처 16곳 병렬 검색 → 사이즈 교차 매칭(sell_now>0 필수, in_stock 기본 False) → 수익 분석. BRAND_SOURCES 브랜드 필터(Nike/Jordan 등). 웍스아웃 등 모델번호 없는 소싱처는 이름 매칭(한→영 변환+키워드 교차+콜라보/서브타입 검증)
- `src/scanner.py` — 카테고리 스캔, 키워드 스캔 오케스트레이터
- `src/tier1_scanner.py` — 워치리스트 빌더. 역방향 + 카테고리 gap 스크리닝
- `src/tier2_monitor.py` — watchlist 실시간 크림 시세 폴링. sell_now_price(즉시판매가) 기준 수익 계산, 5배 안전망(오매칭 방어)
- `src/continuous_scanner.py` — next_scan_at 기반 47k 연속 배치 스캔

### 크롤러 (16개 소싱처)
- `src/crawlers/musinsa_httpx.py` — 무신사. API 검색 (`caller=SEARCH`), 세션 쿠키 등급할인가
- `src/crawlers/twentynine_cm.py` — 29CM. 검색 API v4/products + HTML 파싱
- `src/crawlers/nike.py` — 나이키 공식몰. `__NEXT_DATA__` JSON 파싱 (selectedProduct 구조). LAUNCH 상품 자동 스킵
- `src/crawlers/adidas.py` — 아디다스 공식몰. taxonomy API (Akamai WAF, Referer 필수)
- `src/crawlers/kasina.py` — 카시나. NHN shopby API. Nike/adidas EXACT(`productManagementCd`), NB는 브랜드 덤프+regex. 사이즈별 재고(`saleType`+`forcedSoldOut`) 직접 노출
- `src/crawlers/abcmart.py` — 그랜드스테이지/온더스팟. a-rt.com 멀티채널 API, prefix 검색 + 상세 API 모델번호 보강
- `src/crawlers/tune.py` — 튠. Shopify Storefront GraphQL API (GET), variant title에서 모델번호/사이즈 파싱
- `src/crawlers/eql.py` — EQL. 한섬 편집숍. HTML 파싱 (검색 godNm 속성 + 상세 sizeItmNm/onlineUsefulInvQty)
- `src/crawlers/nbkorea.py` — 뉴발란스 공식몰. 카테고리 SSR 매핑 + getOtherColorOptInfo GET API
- `src/crawlers/salomon.py` — 살로몬 공식몰. Shopify products.json REST API. SKU=크림 모델번호(L+8자리), handle 직접 조회
- `src/crawlers/arcteryx.py` — 아크테릭스 코리아. api.arcteryx.co.kr Laravel REST API. 검색+옵션(사이즈/재고) 조합
- `src/crawlers/vans.py` — 반스 공식몰. Topick Commerce 플랫폼. 검색 JSON API + HTML data-sku-data 사이즈별 재고 파싱
- `src/crawlers/wconcept.py` — W컨셉. POST 검색 API (gw-front, DISPLAY-API-KEY) + GET 상세 HTML 파싱 (brazeJson/skuqty)
- `src/crawlers/worksout.py` — 웍스아웃. REST API 검색(사이즈/재고 포함) + 상세. 모델번호 없음(역방향 이름 매칭)
- `src/crawlers/registry.py` — 레지스트리 + 서킷브레이커 (3회 실패 → 30분 비활성화)

### 크림 데이터
- `src/crawlers/kream.py` — curl_cffi Safari 핑거프린트 + Nuxt `__NUXT_DATA__` 파싱 (sizes, prices, trade volume)
- `src/kream_realtime/collector.py` — 신규 상품 자동 수집 (6시간 주기)
- `src/kream_realtime/price_refresher.py` — hot 전용 시세 갱신 (30분 주기). 3회 연속 실패 → cold 강등 (refresh_fail_count)
- `src/kream_realtime/volume_spike_detector.py` — 거래량 급등 감지 (2배 이상 → hot 승격). 체크 시 volume_7d+refresh_tier+scan_priority 모두 갱신

### 매칭/수익
- `src/matcher.py` — 모델번호 정규화 + exact match. fuzzy 없음. 콜라보 키워드 감지 + 서브타입(PRM/QS/SE 등) 필터
- `src/profit_calculator.py` — 크림 수수료 차감 후 수익/ROI/시그널 판정
- `src/scan_cache.py` — 모델번호 중복 스캔 방지 (일반 24h, 역방향 2h, 수익 6h TTL)

### 인프라
- `src/scheduler.py` — discord.ext.tasks 루프 6개 (Tier1/Tier2/일일리포트/수집/갱신/급등)
- `src/watchlist.py` — watchlist.json 기반 모니터링 대상
- `src/models/database.py` — Async SQLite (aiosqlite)
- `src/config.py` — Pydantic BaseSettings, `.env` 로드
- `src/discord_bot/bot.py` — 슬래시 명령, embed 알림, 6시간 중복 알림 방지 (시그널 업그레이드/수익 20%↑ 시 재전송)

## 크림 API (GET 전용)

```
/api/p/e/search/products       — 키워드 검색 (sort=date, page, per_page)
/api/p/e/products/{id}         — 상품 상세
/api/p/options/display?product_id={id}  — 사이즈별 시세
/api/p/e/products/{id}/sales   — 거래 내역
```

## 소싱처 Rate Limit

| 소싱처 | max_concurrent | min_interval | 시간당 안전 처리량 |
|--------|:-:|:-:|:-:|
| 무신사 | 5 | 1.0초 | ~3,600건 |
| 29CM | 2 | 2.5초 | ~1,440건 |
| 나이키 | 2 | 3.0초 | ~1,200건 |
| 아디다스 | 2 | 5.0초 | ~720건 |
| 카시나 | 2 | 1.5초 | ~2,400건 |
| 그랜드스테이지 | 3 | 2.0초 | ~1,800건 |
| 온더스팟 | 3 | 2.0초 | ~1,800건 |
| 튠 | 2 | 1.0초 | ~3,600건 |
| EQL | 2 | 1.5초 | ~2,400건 |
| 뉴발란스 | 2 | 2.0초 | ~1,800건 |
| 살로몬 | 2 | 1.0초 | ~3,600건 |
| 아크테릭스 | 2 | 2.0초 | ~1,800건 |
| 반스 | 2 | 1.5초 | ~2,400건 |
| W컨셉 | 2 | 2.0초 | ~1,800건 |
| 웍스아웃 | 2 | 2.0초 | ~1,800건 |

## 수수료 계산

```
정산가 = 판매가 - (기본료 2,500 + 판매가 × 6%) × 1.1(VAT) - 배송비 3,000
검수비 0원
```

### 시그널 기준
- STRONG_BUY: 순수익 ≥ 30,000 AND 7일 거래량 ≥ 5
- BUY: 순수익 ≥ 15,000 AND 7일 거래량 ≥ 5
- WATCH: 순수익 ≥ 5,000 AND 7일 거래량 ≥ 3
- NOT_RECOMMENDED: 그 외

### 알림 하드 플로어
- 순수익 ≥ 10,000₩ AND ROI ≥ 5% AND 거래량 ≥ 1

## 개발 자동화

### 서브에이전트 (`.claude/agents/`)

| 에이전트 | 역할 | 권한 |
|---------|------|------|
| `source-analyzer` | 소싱처 종합 분석 — 덤프/재고/매칭/기법 판별 | Read + Bash(GET) |
| `api-prober` | 소싱처 API 탐색 + 최적 기법 판별 (httpx/curl_cffi/Playwright) | Read + Bash(GET) |
| `crawler-builder` | 소싱처별 최적 기법으로 크롤러 풀사이클 구현 | Edit/Write |
| `catalog-dumper` | 카탈로그 전체 덤프 + 크림 DB 매칭 엔진 구현 (푸시 방식) | Edit/Write |
| `verify-agent` | verify.py + pytest, 실패 시 자동 수정 (3회) | Edit/Write |
| `code-reviewer` | 보안/성능/품질 코드 리뷰 | Read only |
| `live-tester` | 크롤러 실서버 end-to-end 검증 (검색→상세→매칭) | Read + Bash(GET) |
| `profit-analyzer` | 수익 계산 검증, 임계값 최적화 | Read + Bash(SELECT) |
| `kream-monitor` | 크림 DB 거래량/시세 모니터링 | Read + Bash(SELECT) |
| `coverage-analyzer` | 크림 DB 대비 소싱처별 커버율/갭 분석 | Read + Bash(SELECT) |
| `reverse-scanner` | 역방향 소싱처 가격 조회 + 수익 탐색 | Read + Bash(GET) |
| `queue-inspector` | 스캔 큐 상태 진단, 적체/소화율 | Read + Bash(SELECT) |
| `scan-debugger` | 상품별 스캔 파이프라인 추적 | Read + Bash(GET+SELECT) |

### 슬래시 명령 (`.claude/commands/`)

| 명령 | 역할 |
|------|------|
| `/commit` | git add + 메시지 생성 + commit + push |
| `/verify` | verify.py + pytest + AST 문법 검증 |
| `/status` | DB 현황 + 워치리스트 + 최근 알림 |
| `/queue` | 스캔 큐 현황 — priority별 분포, 적체, ETA |
| `/trace` | 특정 모델번호 스캔 파이프라인 추적 |
| `/health` | 소싱처 16곳 헬스체크 — 응답시간, 서킷브레이커, 기법별 상태 |
| `/add-source` | URL 하나로 소싱처 추가 (분석→구현→테스트→커밋) |
| `/catalog` | 소싱처별 카탈로그 현황 — 덤프 시각, 상품 수, 매칭율 |
| `/coverage` | 크림 DB 대비 소싱처 커버율 — 브랜드별 갭, 추가 제안 |

### PostToolUse 훅
- Edit/Write 후 자동 pytest 실행 → 실패 시 즉시 수정 루프

## Discord 명령어

- `!역방향스캔` — hot 상품 역방향 스캔 (테스트: 5개, 전체: `!역방향스캔 전체`)
- `!카테고리스캔 [카테고리] [페이지]` — 무신사 카테고리 전수 대조
- `!자동스캔` / `!강제스캔` — Tier1+Tier2 자동/즉시 실행
- `!배치스캔` — 크림 DB 순회 스캔
- `!상태` — 봇 상태 + 서킷브레이커 + 캐시/알림 통계
- `!워치리스트` — 워치리스트 상위 10건

## Configuration

`.env` 주요 변수:
- `DISCORD_TOKEN`, `CHANNEL_PROGRESS`, `CHANNEL_PROFIT_ALERT`, `CHANNEL_LOG`
- `MUSINSA_EMAIL/PASSWORD` — 등급할인가 수집용
- `TIER1_INTERVAL_MINUTES=30`, `TIER2_INTERVAL_SECONDS=60`, `HTTPX_CONCURRENCY=10`

## Project Rules

### 읽기 전용(Read-Only) 원칙
- **핵심 원칙**: 읽기 전용이면 수단 제한 없음. 상태 변경만 금지
- **소싱처별 최적 기법 선택**: 분석 후 가장 적합한 방법을 바로 적용
  - `httpx`: 공개 API, 차단 없는 사이트
  - `curl_cffi`: TLS fingerprint 차단 사이트 (크림에서 검증 완료)
  - `Playwright`: JS 렌더링 필수, NetFunnel 큐 등 브라우저 필수 사이트
  - `모바일 API`: 웹 차단이지만 앱 API 열린 사이트
- **GET 허용**: 검색, 상세, 재고 등 모든 읽기 요청
- **POST 허용**: 검색/필터/GraphQL 쿼리 등 읽기 목적 요청만
- **POST 금지**: 주문/결제/로그인/장바구니/위시리스트 등 상태 변경
- PUT/DELETE 요청 금지
- API 호출 간 최소 1~2초 딜레이

### 크롤링 안전
- 크림: Hidden API, 429 → 30초 대기 재시도. 500 에러 시 서버 장애 → 재시도 후 포기
- 크림 API 호출 최소화: NUXT 우선 → API 1회 fallback, cold는 경량 options/display API만 사용
- 무신사: API 검색 (`caller=SEARCH`), 세션 쿠키 등급할인가
- 47k 전체 시세 갱신 절대 금지 — hot tier만 price_refresher, cold는 연속 스캔 시 즉석 조회

### 데이터 처리
- 사이즈 파싱: `isinstance(data, dict)` 체크 필수
- 모델번호: 무신사 SKU가 아닌 상품명에서 실제 품번 추출
- `sqlite3.Row`는 `.get()` 불가 — `dict()`로 변환
- 크림 API 404는 에러 아님 (거래량 0 신규 상품)

### 필터링
- 브랜드 필터: **블랙리스트 방식** — 크림에 절대 없는 브랜드만 스킵
- 오프라인전용: "오프라인 전용 상품" 정확 문구 + 구매버튼 없을 때만 스킵
- 발매예정: "판매예정" 또는 "출시예정" → 스킵

### 필수 에이전트 투입 규칙
- 새 소싱처 추가 → `crawler-builder` 에이전트 투입 필수 (API 탐색 → 구현 → 테스트 풀사이클)
- 수익 계산 로직 변경 (`profit_calculator.py`, 수수료, 시그널 기준) → `profit-analyzer` 검증 필수
- DB 스키마 변경 → `kream-monitor`로 마이그레이션 전후 데이터 무결성 확인

### 개발 방식
- Plan 모드: 큰 작업은 설계 먼저, 승인 후 실행
- 커밋: 작업 완료 시 git commit + 디스코드 웹훅 알림
- 웹훅 URL: https://discord.com/api/webhooks/1490976364868931704/vhyCn1X42x3OBIz1GvcNCVuiGSOjOdADkF4ttkcCIskIR_fnmzZkRCu9MYptN1dxFWN3

### 버그 수정 원칙
- 3회 실패 시 방법 지정 금지 — 문제+제약+실패이력만 제공, Claude Code가 탐색
- 단위 테스트만으로 검증 금지 — 해당 버그 재현 테스트가 통과해야 검증
- 검증 실패 시 사람 개입 없이 재수정→재검증

### 자동 검증 루프
1. `PYTHONPATH=. python scripts/verify.py` — 파이프라인 검증
2. `pytest tests/ -v` — 전체 테스트
3. `python3 -c "import ast; ast.parse(open('<파일>').read())"` — 문법 검증
4. 검증 성공 시에만 git commit

### 회귀 테스트
- `tests/fixtures/false_positives.json`에 케이스 추가
- `status: "known_bug"` → 수정 후 `"fixed"`

### 장애 격리 (서킷브레이커)
- `registry.py`: 연속 3회 실패 → 30분 비활성화, 자동 재활성화
- 소싱처별: 무신사(안정), 29CM(안정), 나이키(안정), 아디다스(WAF 주의), 카시나(안정), 그랜드스테이지(안정), 온더스팟(안정), 튠(안정), EQL(안정), 뉴발란스(안정), 살로몬(안정), 아크테릭스(안정), 반스(안정), W컨셉(안정), 웍스아웃(안정)

## Dev Environment

WSL2 + Windows. 무신사 세션 쿠키: `data/musinsa_session.json`.

### Code Style
- Ruff: line-length 100, rules E/F/I/N/W, target Python 3.13
- pytest: `asyncio_mode = "auto"`
- All I/O async (aiosqlite, aiohttp, httpx)
- 한국어: 사용자 메시지, Discord, 문서 / 영어: 코드 식별자

### Known Issues
- MFS(다중재고) 품절 필터 한계 — inventory API 근본 미작동 (MT410CK5 등)
- 크림 거래량 5건 캡 — pinia/screens 모두 최대 5건 반환, ×3 추정치로 보충
