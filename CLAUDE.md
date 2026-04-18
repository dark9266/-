# CLAUDE.md

크림봇 (Kream Monitor Bot) — 크림 차익거래 자동화 Discord 봇. Python 3.12+, async-first. 개인용 초고성능 (배포 A).

## 🔴 INVARIANT — 매 세션 맨 먼저 읽을 것 (변경 금지)

**단일 source of truth**: `~/.claude/projects/-mnt-c-Users-USER-Desktop----/memory/project_current_track.md`
과거 로그: `project_history_archive.md` (자동 로드 X, 수동 참조만).

### 목표 ↔ 방법 (세션 표류 방지 핵심)
- **목표**: 크림 47k DB **전체 상품 (전 카테고리, 거래량 0 포함)** 을 각 소싱처에서 찾아 수익 실현. **크림에 없는 건 무시.**
- **방법**: **푸시** — 소싱처 카탈로그 전체 덤프 → 크림 DB 로컬 교집합 → 후보만 크림 sell_now → 수익 → 알림.
- **왜 푸시**: 목표가 "역방향 맛"이어도 역방향 실행은 크림 캡 폭발. 푸시가 유일한 방법.

### 🎯 타겟 범위 (축소 금지)
- **47k 전부**: "hot 130건만", "거래량 ≥5만", "인기 카테고리만", "신발만" 축소 **전부 금지**
- **거래량 0~3 포함**: 숨은 보석 = 거래량 낮은 상품의 가격 급등 마진이 가장 큼
- **축 ② 예외**: `tier2_monitor` hot 폴링은 보조 감시. 메인 타겟 축소 아님.
- **필터 vs 축소 구분**: 알림 하드 플로어(순수익/ROI/거래량)는 "거짓 알림 방지"지 "타겟 축소" 아님.

### 🚨 다음 세션 금지 질문/제안
- "역방향이 원래 의도 아니냐?" → **X.** 목표는 교집합, 방법은 푸시.
- "hot/인기 카테고리 먼저 타겟팅할까요?" → **X.** 47k 전체 고정.
- "거래량 낮은 건 빼도 되지 않나요?" → **X.** 숨은 보석 핵심 차별화.

### 완성도 축 (우선순위 고정)
1. **정확성** — 매칭 + 사이즈별 실재고. 거짓 알림 0 지향.
2. **속도** — 소싱처 서칭 속도.
3. **신경 X**: 알림 수량 · 무인 운영 · 상용화.

### 단계 (순서 뒤집지 말 것)
1. **현재 = 22 소싱처 안정화** (테스트 사이클 1h → 2h → 6h → 12h → 24h 점진)
2. **안정화 확정 후 = 소싱처 대거 확장**

### 🚫 금지 리스트
- 역방향 스캐너 재설계·`continuous_scanner`·`tier1_scanner`·cold/warm 순환 (폐기 흐름)
  - **예외**: `tier2_monitor.py` (축 ② 보조 감시) 는 역방향 hot 폴링이지만 **유지**. "역방향이니 끄자" 제안 금지 — 이미 판정 완료된 예외다.
- 소싱처 신규 추가 (안정화 전까지)
- 해외 스토어 (EU/US/JP) — 한국 공홈만
- 매칭량 숫자 쫓기 — 헬스체크 4종 통과 전에는 매칭 작업 금지
- 선택지 a/b/c 나열 — 필수 작업 1개만 제시 (사용자 배달일 중 원격 지시)
- 라이브 관측 없이 수치 주장
- 크림 시세/거래량 로컬 DB로 수익 계산 (반드시 실시간 sell_now)

### ✅ 매 세션 시작 헬스체크 4종 (생략 금지)
1. 봇 프로세스 살아있나 (`ps -ef | grep python.*main`)
2. 마지막 알림 24h 내 (`alert_sent.fired_at`)
3. 크림 일일 호출 KREAM_DAILY_CAP 이하 (`kream_api_calls` 24h, .env 기준)
4. 파이프라인 활동 — `decision_log` 최근 2h 내 `dedup_recent|prefilter_unprofitable|profit_emitted|alert_sent` 합 > 0 (※ `kream_collect_queue` 는 미등재 신상 보조 채널이라 공백 정상)

**1개라도 FAIL → 매칭/신규 작업 착수 금지, 복구 먼저.**

### 메인 아키텍처
- `src/adapters/*_adapter.py` (22곳) → `src/core/event_bus` → `src/core/orchestrator` → `kream_collect_queue` → 알림
- 축 ② 보조: `tier2_monitor.py` 역방향 hot 130건 60초 폴링 (폐기 X, 재포지셔닝)
- 아래 "v2 Architecture" 섹션은 **참조용 구조 설명**이지 현행 메인 아님.

## Commands

```bash
pip install -e ".[dev]"          # 설치
python main.py                   # 봇 실행
pytest tests/ -v                 # 전체 테스트
PYTHONPATH=. python scripts/verify.py  # 파이프라인 검증
ruff check src/ tests/           # 린트
ruff format src/ tests/          # 포맷
```

## 현행 아키텍처 (푸시 단일 트랙)

```
22 소싱처 어댑터 (src/adapters/*) → 카탈로그 덤프
  → matcher (크림 DB 로컬 교집합)
  → collect_queue (크림 후보)
  → kream_delta_watcher + orchestrator (sell_now 조회)
  → profit_calculator
  → Discord 알림
```

**축 ② 보조**: `tier2_monitor.py` — 크림 hot 거래량 ≥5 상품 60초 폴링. 유지 (폐기 X).

> **과거 v2 (Tier 0/1, hot/warm/cold 순환, 역방향 주력) 는 폐기 흐름**.
> `continuous_scanner.py` · `tier1_scanner.py` · hot/warm/cold 큐 컬럼 · `next_scan_at` / `scan_priority` 는 **현행 아키텍처와 무관**.
> 상세 v2 구조 필요 시 `project_history_archive.md` 참조 — 리팩터링/개선/재활용 제안 금지.

## Key Modules

### 스캐너 (현행)
- `src/tier2_monitor.py` — **축 ② 유지**. watchlist 실시간 크림 시세 폴링. sell_now_price(즉시판매가) 기준 수익 계산, 5배 안전망(오매칭 방어)

### 🗑 폐기 흐름 스캐너 (참조용, 손대지 말 것)
- `src/reverse_scanner.py` · `src/scanner.py` · `src/tier1_scanner.py` · `src/continuous_scanner.py` — v2 시절 역방향/Tier 구조. 현행 푸시 트랙과 무관. 리팩터링·버그픽스·재활용 제안 금지.

### 크롤러 (22개 소싱처 — 2026-04-14 stussy 추가)
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
- `src/adapters/stussy_adapter.py` — Stussy 한국 공식몰 (kr.stussy.com, Shopify). `/products.json` 페이지네이션. variant.sku digit prefix → 크림 prefix 인덱스 매칭. 다중 후보 시 영문→한글 색상 사전으로 disambiguation. 크림 Stussy 2,389행 풀 활성화 (2026-04-14 추가)
- `src/adapters/{patagonia,beaker,thehandsome,puma,asics,nike,adidas,thenorthface}_adapter.py` — Phase 3 배치 어댑터들 (직접 httpx/Shopify 기반 푸시 어댑터, crawler 레이어 없이 어댑터에 통합)
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

### 시그널 기준 (2026-04-15 숨은 보석 정책)
- STRONG_BUY: 순수익 ≥ 30,000 AND 7일 거래량 ≥ **1**
- BUY: 순수익 ≥ 15,000 AND 7일 거래량 ≥ **1**
- WATCH: 순수익 ≥ 5,000 AND 7일 거래량 ≥ **1**
- NOT_RECOMMENDED: 그 외 (거래량 0 = 대기 매수자 없음 → 판매 불가)

**거래량 게이트 1 고정**. 저거래 상품(숨은 보석)이 핵심 차별화 — 낮추자 제안 금지. 거짓 알림 방어는 순수익/ROI 하드 플로어 담당.

### 알림 하드 플로어
- 순수익 ≥ 10,000₩ AND ROI ≥ 5% AND 거래량 ≥ 1

## 개발 자동화

### 서브에이전트 (`.claude/agents/`) — 의무 투입 규칙

**원칙**:
1. 아래 도메인 트리거 충족 시 → **의무** 투입 (필수).
2. 그 외 상황에서 유저가 매번 "필요시 투입해죠"를 말하지 않아도, 다음 조건이면 **자율** 투입 허용:
   - 읽기 전용 코드베이스 탐색·진단 (Glob/Grep/Read 다회 필요, 결과 합성 필요)
   - 상호 독립적 다중 조사 → **병렬**로 돌릴 수 있는 작업
   - 메인 컨텍스트에 대용량 로그/결과물이 쏟아질 우려가 있는 탐색
3. **자율 투입 금지 상황** (항상 사전 확인):
   - 실제 코드 수정·커밋·푸시·DB 스키마 변경 등 상태 변경 동반 작업
   - 도메인 의무 트리거(아래 표)와 겹치는 작업은 도메인 에이전트 우선
   - 파일 1~2개만 보면 되는 단건 조사는 직접 실행이 더 빠름 — 굳이 투입 금지
4. 자율 투입 시 **무엇을/왜 띄웠는지 한 줄 고지** (user 가 cancel 판단 가능하도록).

| 에이전트 | 역할 | 의무 투입 트리거 |
|---------|------|-----------------|
| `crawler-builder` | 소싱처 크롤러 풀사이클 구현 | 새 소싱처 추가 시 **필수** |
| `code-reviewer` | 보안/성능/품질 코드 리뷰 | 코어 모듈 수정 완료 시 (kream.py, runtime.py, profit_calculator.py, orchestrator.py) |
| `scan-debugger` | 상품별 파이프라인 전 구간 추적 | "왜 안 잡혀?", "왜 알림 안 와?" 류 디버깅 |
| `live-tester` | 크롤러 실서버 e2e 검증 | 크롤러/어댑터 수정 후 실동작 검증 |
| `profit-analyzer` | 수익 계산/수수료 검증 | 수수료·시그널·하드플로어 변경 시 **필수** |

**JIT 대기 (소싱처 확장 단계 활성화)**:
| 에이전트 | 역할 |
|---------|------|
| `api-prober` | 소싱처 API 탐색 + 최적 기법 판별 |
| `source-analyzer` | 소싱처 종합 분석 — 덤프/재고/매칭 방식 판별 |

**폐기 (2026-04-16)**: security-guard, catalog-dumper, delta-engine-builder, pipeline-builder, runtime-sentinel, verify-agent, kream-monitor, coverage-analyzer, queue-inspector — 직접 실행이 더 빠른 단순 작업이거나 사용 단계 미도달.

### 슬래시 명령 (`.claude/commands/`)

| 명령 | 역할 |
|------|------|
| `/commit` | git add + 메시지 생성 + commit + push |
| `/verify` | verify.py + pytest + AST 문법 검증 |
| `/status` | DB 현황 + 워치리스트 + 최근 알림 |
| `/queue` | 스캔 큐 현황 — priority별 분포, 적체, ETA |
| `/trace` | 특정 모델번호 스캔 파이프라인 추적 |
| `/health` | 소싱처 22곳 헬스체크 — 응답시간, 서킷브레이커, 기법별 상태 |
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
- 웹훅 URL: `.env`의 `DISCORD_NOTIFY_WEBHOOK` 사용 (docs/security/incident-2026-04-13.md 참조)

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
