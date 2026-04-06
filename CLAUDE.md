# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

크림봇 (Kream Monitor Bot) — Discord bot that monitors Kream (Korean sneaker resale platform) and Musinsa (Korean fashion retailer) to find arbitrage opportunities. Written in Python 3.12+ with async-first design.

## Commands

```bash
# Setup
pip install -e ".[dev]"

# Run the bot
python main.py

# Tests
pytest tests/                        # all tests
pytest tests/test_matcher.py -v      # single file
pytest tests/test_auto_scan_live.py -v -s  # E2E (requires network)

# Lint
ruff check src/ tests/
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Architecture

**Entry point:** `main.py` → initializes DB, Scanner, Scheduler, connects Discord bot.

**Core pipeline:** Scanner orchestrates the flow:
```
Multi-Source Crawlers (무신사/29CM/ABC마트/나이키/아디다스) → Matcher (model# matching) → Profit Calculator → Discord Alerts
```

**Key modules:**

- `src/scanner.py` — Orchestrator. Runs keyword scans, auto-scans (Kream popular → Musinsa search → profit analysis), reverse scans (Musinsa sales → Kream DB match), and batch scans. Uses 3-stage Musinsa search: model# → product name → brand+name.
- `src/crawlers/kream.py` — Parses Kream's Nuxt `__NUXT_DATA__` (devalue format) for sizes, prices, trade volume.
- `src/crawlers/musinsa_httpx.py` — httpx 기반 무신사 크롤러. 세션 쿠키(`data/musinsa_session.json`)로 등급할인가 수집.
- `src/crawlers/twentynine_cm.py` — 29CM 크롤러. 검색 API v4/products + HTML 파싱 (schema.org + RSC payload).
- `src/crawlers/abcmart.py` — ABC마트 크롤러. JSON API (검색 /display/search-word/result-total/list, 상세 /product/info).
- `src/crawlers/nike.py` — 나이키 공식몰 크롤러. __NEXT_DATA__ JSON 파싱 (2024+ selectedProduct 구조).
- `src/crawlers/adidas.py` — 아디다스 공식몰 크롤러. taxonomy API (/api/search/taxonomy, 사이즈/가격 포함).
- `src/crawlers/registry.py` — 소싱처 크롤러 레지스트리. 서킷브레이커: 연속 3회 실패 시 30분 비활성화, 자동 재활성화.
- `src/matcher.py` — Model number normalization and exact matching (e.g., "dq8423 100" → "DQ8423-100"). No fuzzy matching.
- `src/profit_calculator.py` — Kream fee structure (base 2500₩ + 6% + 10% VAT), per-size profit/ROI, signal determination (STRONG_BUY 30k+ / BUY 15k+ / WATCH 5k+ / NOT_RECOMMENDED).
- `src/scheduler.py` — `discord.ext.tasks` 3개 루프: Tier1 워치리스트 빌더(30분), Tier2 실시간 폴링(60초), 일일 리포트(자정).
- `src/watchlist.py` — watchlist.json 기반 모니터링 대상 관리. 멀티소스 지원 (source, source_price, source_url 필드).
- `src/tier1_scanner.py` — 워치리스트 빌더. 리스팅 가격 기반 gap 스크리닝 + 멀티소스 최저가 비교.
- `src/tier2_monitor.py` — 실시간 크림 시세 폴링. 수익 조건 도달 시 알림.
- `src/utils/rate_limiter.py` — AsyncRateLimiter (Semaphore + 최소 간격).
- `src/discord_bot/bot.py` — 16+ slash commands, rich embed alerts, 1-hour alert dedup cooldown.
- `src/models/database.py` — Async SQLite (aiosqlite), 11 tables: products, price history, trade volume, alerts, keywords, settings.
- `src/config.py` — Pydantic `BaseSettings`, loads from `.env`.
- `src/scan_cache.py` — JSON 기반 모델번호 스캔 캐시. 24시간 TTL (수익 발생 시 6시간). 중복 스캔 방지.
- `src/kream_realtime/collector.py` — 크림 신규 상품 자동 수집 (카테고리별 date순 검색, 6시간 주기).
- `src/kream_realtime/price_refresher.py` — 거래량 기반 우선순위 시세 갱신 (hot 30분/cold 6시간, 배치 큐).
- `src/kream_realtime/volume_spike_detector.py` — 거래량 급등 감지 (스냅샷 비교, 2배 이상 → hot 승격).

### 실시간 DB 레이어

3개 루프가 `scheduler.py`에서 독립 실행:

| 루프 | 주기 | 모듈 | 역할 |
|------|------|------|------|
| `collect_loop` | 6시간 | `src/kream_realtime/collector.py` | 카테고리별 신규 상품 수집 (date순) |
| `refresh_loop` | 10분 | `src/kream_realtime/price_refresher.py` | 우선순위 큐 시세 갱신 (hot 30분/cold 6시간) |
| `spike_loop` | 60분 | `src/kream_realtime/volume_spike_detector.py` | 거래량 급등 감지 + hot 승격 |

**Tier 분류:**
- `hot`: 7일 거래량 >= 5 → 30분마다 시세 갱신
- `cold`: 7일 거래량 < 5 → 6시간마다 시세 갱신
- 급등 감지 시 cold → hot 자동 승격

**DB 확장:**
- `kream_products`: +`volume_7d`, `volume_30d`, `last_volume_check`, `refresh_tier`, `last_price_refresh`
- `kream_volume_snapshots`: 거래량 시계열 스냅샷 (급등 비교용)

**API (GET 전용):**
- `/api/p/e/search/products` (keyword, sort=date, page, per_page) — 신규 수집
- `/api/p/e/products/{id}` — 상세
- `/api/p/options/display?product_id={id}` — 사이즈별 시세
- `/api/p/e/products/{id}/sales` — 거래 내역

## 개발 자동화 인프라

### Stop 훅 (`.claude/settings.json`)
- `Edit`/`Write` 도구 실행 후 자동으로 pytest 실행
- 실패 시 Claude Code가 즉시 수정 루프 진입

### 슬래시 명령 (`.claude/commands/`)
- `/commit` — git add + 메시지 자동 생성 + commit + push
- `/verify` — verify.py + pytest + 문법 검증 파이프라인
- `/status` — DB 현황 + 워치리스트 + 최근 알림 대시보드

### 서브에이전트 (`.claude/agents/`)
- `verify-agent` — verify.py + pytest 전담, 실패 시 자동 수정 (최대 3회)
- `api-prober` — 새 소싱처 API 탐색 (GET 전용, 응답 구조 문서화)
- `code-reviewer` — 코드 리뷰 전담 (보안/성능/품질 체크리스트)
- `kream-monitor` — 크림 DB 거래량/시세 모니터링 (SELECT 전용)
- `crawler-builder` — 새 소싱처 크롤러 풀사이클 구현 (API 탐색 → httpx 코드 → 테스트 → 검증)
- `profit-analyzer` — 수익 계산 검증 + 알림 임계값 최적화 (SELECT 전용)
- `reverse-scanner` — 크림 hot 상품 기준 역방향 소싱처 가격 조회 + 수익 기회 탐색 (GET 전용)

## Configuration

Environment variables in `.env` (see `.env.example`):
- `DISCORD_TOKEN`, `CHANNEL_*` — Discord bot token and channel IDs
- `MUSINSA_EMAIL/PASSWORD` — Optional auto-login credentials
- Thresholds: `AUTO_SCAN_CONFIRMED_ROI=5.0`, `AUTO_SCAN_ESTIMATED_ROI=10.0`
- Tier settings: `TIER1_INTERVAL_MINUTES=30`, `TIER2_INTERVAL_SECONDS=60`, `HTTPX_CONCURRENCY=10`

## 장애 격리 (서킷브레이커)

- `src/crawlers/registry.py`: 연속 3회 실패 → 30분 비활성화, 기간 만료 시 자동 재활성화
- `src/tier1_scanner.py`: `get_active()`로 활성 크롤러만 검색, `record_failure()`/`record_success()` 추적
- `src/tier2_monitor.py`: 알림 발송 실패 시 `result.errors` 증가
- 소싱처별 안정성:
  - 무신사: 안정 (API 검증 완료)
  - 29CM: 안정 (검색 API v4/products 엔드포인트, 2024-04 검증)
  - 나이키: 안정 (검색 Wall + PDP selectedProduct.sizes 2024+ 구조 적용)
  - ABC마트: 안정 (JSON API 전환 — 검색 /display/search-word/result-total/list, 상세 /product/info)
  - 아디다스: 안정 (taxonomy API 전환 — /api/search/taxonomy, Akamai WAF 우회 성공)

## Code Style

- Ruff: line-length 100, rules E/F/I/N/W, target Python 3.13
- pytest: `asyncio_mode = "auto"` (async tests auto-detected)
- All I/O is async (aiosqlite, aiohttp, httpx)
- Korean used in user-facing strings, Discord messages, and documentation; English in code identifiers

## Dev Environment

WSL2 + Windows: bot runs on Linux. 무신사 세션 쿠키는 `data/musinsa_session.json`에서 로드.

## 현재 명령어 체계

- `!역방향스캔` — 브랜드별 무신사 검색 → 크림 DB 매칭 (TOP 20 브랜드, 테스트 모드 5개)
- `!역방향스캔 전체` — 전체 브랜드 무제한
- `!카테고리스캔 [카테고리] [페이지]` — 무신사 카테고리 페이지 전수 대조 (개발 중)
- `!자동스캔` — Tier1+Tier2 자동 실행
- `!배치스캔` — 크림 DB 순회 스캔
- `!상태` — 봇 상태 + 소싱처 서킷브레이커 + 캐시/알림 통계
- `!워치리스트` — 워치리스트 상위 10건 (모델번호, 소싱처, 가격, 갭)
- `!강제스캔` — Tier1 즉시 실행 (30분 주기 무시)

## 역방향 스캔 동작 흐름

1. 브랜드별 무신사 검색 API (aiohttp) → 상품 리스트
2. 상품별 상세 조회 (httpx) → 사이즈+가격
3. 사이즈 파싱: options API + inventory API
4. 품절 필터: inventory v2 API + productVariantId 매핑
5. 크림 DB SQLite 매칭 (모델번호 인덱스) → 없으면 크림 API 호출
6. 수수료 차감 후 수익 계산 → 디스코드 알림

## 카테고리 스캔 (개발 중)

- 무신사 카테고리 리스팅 API → 모델번호 추출 → 크림 DB 전수 대조
- 상세 페이지 방문은 매칭 성공 시에만 (속도 최적화)
- 목표: 브랜드 제한 없이 전체 크롤링 (캐찹매니아 방식)

## Project Rules (실수 방지용 — 반드시 준수)

### 크롤링 안전
- 무신사: 요청 간격 2초+, 로그인 세션으로 등급할인가 수집
- 크림: 공개 API 없음 (Hidden API 사용), 요청 간격 2초+, 429 받으면 30초 대기 후 재시도
- 47k 전체 시세 갱신 절대 금지 — 매칭 성공 상품만 갱신

### 데이터 처리
- 사이즈 파싱: isinstance(data, dict) 체크 필수. list면 변환 후 처리
- 모델번호: 무신사 SKU(NBPDGS111G_15)가 아닌 상품명에서 실제 품번(U7408PL) 추출
- sqlite3.Row는 .get() 불가 — dict()로 변환 후 사용
- 크림 API 404는 에러 아님 (거래량 0인 신규 상품)

### 필터링
- 브랜드 필터: **블랙리스트 방식** (`MUSINSA_ONLY_BRANDS`) — 크림에 절대 없는 브랜드만 스킵, 나머지는 모두 상세 방문. 화이트리스트는 과필터링 위험이 높으므로 사용 금지.
- 오프라인전용: "오프라인 전용 상품" 정확 문구 + 구매버튼 없을 때만 스킵
- 발매예정: "판매예정" 또는 "출시예정" 텍스트 → 스킵

### 수수료 계산
- 정산가 = 판매가 - (기본료 2,500 + 판매가 × 6%) × 1.1(VAT) - 배송비 3,000
- 검수비 0원

### 개발 방식
- Plan 모드: 큰 작업은 Shift+Tab 두 번 → 설계 먼저, 승인 후 실행
- 검증: 수정 후 반드시 !역방향스캔 테스트로 수치 검증 (수집 건수, 매칭률, 에러 수)
- 커밋: 작업 완료 시 git commit + 디스코드 웹훅 알림
- 웹훅 URL: https://discord.com/api/webhooks/1488503869183889446/PEqriAS5fVPkdeGsqqzDASVsbd_yYMDy01e5rDIBHh03RbagsvNeb5LClRsuKCpY5ty6

### 버그 수정 원칙
- 3회 실패 시 방법 지정 금지 — 문제+제약+실패이력만 제공, 최적 방법은 Claude Code가 탐색
- 단위 테스트만으로 검증 완료 금지 — 실환경과 동일한 조건으로 검증 (curl 실제 API 호출 → 파싱 → 결과 출력)
- "134 tests passed"는 검증이 아님 — 해당 버그를 재현하는 테스트가 통과해야 검증
- 검증 실패 시 사람 개입 없이 재수정 후 재검증

### 자동 검증 루프
코드 수정 후 커밋 전 반드시 아래 순서로 자동 검증. 사람 개입 없이 실패→재수정→재검증 반복.

1. **파이프라인 검증**: `PYTHONPATH=. python scripts/verify.py` — 알림필터/품절필터/카테고리스캔/수수료 자동 검증
2. **pytest 전체 통과**: `pytest tests/ -v` — 실패 시 원인 분석 후 코드 재수정, 재실행
3. **import 검증**: `python3 -c "import ast; ast.parse(open('<수정파일>').read())"` — 문법 오류 시 즉시 수정
4. **단위 테스트 실행**: 수정 로직 관련 테스트 파일 개별 실행 (`pytest tests/test_<모듈>.py -v`)
5. **검증 성공 시에만** git commit + 디스코드 웹훅 전송
6. **디스코드 실제 테스트** (!역방향스캔, !카테고리스캔 등 실환경 검증)만 사람이 직접 실행

### 회귀 테스트 원칙
- 새 버그 발견 시 `tests/fixtures/false_positives.json`에 케이스 추가
- 케이스 추가만으로 자동 회귀 테스트 확장 (코드 수정 불필요)
- `status: "known_bug"` → 수정 후 `"fixed"`로 변경

### 스캔 캐시 (`src/scan_cache.py`)
- 모델번호 기반 중복 스캔 방지 (`data/scan_cache.json`)
- 일반 상품 24시간, 수익 발생 상품 6시간 TTL
- Scanner 초기화 시 만료 항목 자동 정리
- 카테고리 스캔에서 캐시 히트 시 상세 방문 스킵

### 알림 최소 기준 (하드 플로어)
- `send_profit_alert()`: BUY/STRONG_BUY 시그널만 발송
- `send_auto_scan_alert()`: 순수익 ≥ 10,000₩ AND ROI ≥ 5% AND 거래량 ≥ 1
- `config.py`: `alert_min_profit`, `alert_min_roi`, `alert_min_volume_7d`

## Chrome 제거 원칙
- Selenium/Chrome/CDP 사용 금지
- 모든 KREAM 데이터 수집은 requests/httpx 직접 API 호출로만
- GET 전용 엔드포인트만 사용, 쓰기 요청 금지
- API 호출 간 최소 1~2초 딜레이 (rate limit 방지)

## 2티어 실시간 아키텍처
- 1티어: asyncio 병렬(동시 10개) 워치리스트 빌더 - 30분 주기 + 멀티소스 최저가 비교
- 2티어: watchlist.json 대상 Pinia API 60초 폴링 - 독립 병렬 실행
- 수익 조건 도달 즉시 웹훅 발송
- 소싱처: 무신사, 29CM, ABC마트, 나이키, 아디다스 (레지스트리 자동 등록)

### Known Issues
- MFS(다중재고) 상품 품절 필터 한계 — inventory API 근본 미작동, 15/17까지만 축소 가능 (MT410CK5 등)
