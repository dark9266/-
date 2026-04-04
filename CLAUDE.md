# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

크림봇 (Kream Monitor Bot) — Discord bot that monitors Kream (Korean sneaker resale platform) and Musinsa (Korean fashion retailer) to find arbitrage opportunities. Written in Python 3.12+ with async-first design.

## Commands

```bash
# Setup
pip install -e ".[dev]"
playwright install chromium

# Run the bot
python main.py

# Tests
pytest tests/                        # all tests
pytest tests/test_matcher.py -v      # single file
pytest tests/test_auto_scan_live.py -v -s  # E2E (requires network + Chrome CDP)

# Lint
ruff check src/ tests/
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Architecture

**Entry point:** `main.py` → initializes DB, Scanner, Scheduler, connects Discord bot.

**Core pipeline:** Scanner orchestrates the flow:
```
Musinsa/Kream Crawlers → Matcher (model# matching) → Profit Calculator → Discord Alerts
```

**Key modules:**

- `src/scanner.py` — Orchestrator. Runs keyword scans, auto-scans (Kream popular → Musinsa search → profit analysis), reverse scans (Musinsa sales → Kream DB match), and batch scans. Uses 3-stage Musinsa search: model# → product name → brand+name.
- `src/crawlers/kream.py` — Parses Kream's Nuxt `__NUXT_DATA__` (devalue format) for sizes, prices, trade volume.
- `src/crawlers/musinsa.py` — Playwright-based, requires logged-in session (`data/musinsa_session.json`). Extracts member-only discounts.
- `src/crawlers/chrome_cdp.py` — Connects to Windows Chrome via CDP (remote debugging) to bypass anti-bot. WSL2↔Windows bridge.
- `src/matcher.py` — Model number normalization and exact matching (e.g., "dq8423 100" → "DQ8423-100"). No fuzzy matching.
- `src/profit_calculator.py` — Kream fee structure (base 2500₩ + 6% + 10% VAT), per-size profit/ROI, signal determination (STRONG_BUY 30k+ / BUY 15k+ / WATCH 5k+ / NOT_RECOMMENDED).
- `src/scheduler.py` — `discord.ext.tasks` loops: 30min auto-scan, 10min fast-track, 5min health-check, midnight daily report.
- `src/discord_bot/bot.py` — 16+ slash commands, rich embed alerts, 1-hour alert dedup cooldown.
- `src/models/database.py` — Async SQLite (aiosqlite), 11 tables: products, price history, trade volume, alerts, keywords, settings.
- `src/config.py` — Pydantic `BaseSettings`, loads from `.env`.

## Configuration

Environment variables in `.env` (see `.env.example`):
- `DISCORD_TOKEN`, `CHANNEL_*` — Discord bot token and channel IDs
- `CHROME_PATH`, `CHROME_DEBUG_PORT`, `CHROME_USER_DATA_DIR` — Chrome CDP settings (Windows paths from WSL2)
- `KREAM_EMAIL/PASSWORD`, `MUSINSA_EMAIL/PASSWORD` — Optional auto-login credentials
- Thresholds: `AUTO_SCAN_CONFIRMED_ROI=5.0`, `AUTO_SCAN_ESTIMATED_ROI=10.0`

## Code Style

- Ruff: line-length 100, rules E/F/I/N/W, target Python 3.13
- pytest: `asyncio_mode = "auto"` (async tests auto-detected)
- All I/O is async (aiosqlite, aiohttp, Playwright)
- Korean used in user-facing strings, Discord messages, and documentation; English in code identifiers

## Dev Environment

WSL2 + Windows: Chrome runs on Windows, bot runs on Linux. Chrome CDP bridges the two via `localhost:9222`.

## 현재 명령어 체계

- `!역방향스캔` — 브랜드별 무신사 검색 → 크림 DB 매칭 (TOP 20 브랜드, 테스트 모드 5개)
- `!역방향스캔 전체` — 전체 브랜드 무제한
- `!카테고리스캔 [카테고리] [페이지]` — 무신사 카테고리 페이지 전수 대조 (개발 중)
- `!자동스캔` — 30분 주기 자동 실행
- `!배치스캔` — 크림 DB 순회 스캔 (Chrome 의존성 제거 필요)

## 역방향 스캔 동작 흐름

1. 브랜드별 무신사 검색 API (aiohttp) → 상품 리스트
2. 상품별 상세 조회 (Playwright) → 사이즈+가격
3. 사이즈 파싱: 3단계 폴백 (직접API → 인터셉트 → DOM)
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

1. **pytest 전체 통과**: `pytest tests/ -v` — 실패 시 원인 분석 후 코드 재수정, 재실행
2. **import 검증**: `python3 -c "import ast; ast.parse(open('<수정파일>').read())"` — 문법 오류 시 즉시 수정
3. **단위 테스트 실행**: 수정 로직 관련 테스트 파일 개별 실행 (`pytest tests/test_<모듈>.py -v`)
4. **검증 성공 시에만** git commit + 디스코드 웹훅 전송
5. **디스코드 실제 테스트** (!역방향스캔, !카테고리스캔 등 실환경 검증)만 사람이 직접 실행

### Known Issues
- MFS(다중재고) 상품 품절 필터 한계 — inventory API 근본 미작동, 15/17까지만 축소 가능 (MT410CK5 등)
