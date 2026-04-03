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
