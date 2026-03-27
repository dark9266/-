# 크림봇 (Kream Monitor Bot) — 프로젝트 핸드오프 문서

> 이 문서는 다른 AI 에이전트나 개발자가 이 프로젝트를 이어서 작업할 수 있도록 작성된 완전한 핸드오프 문서입니다.
> 마지막 업데이트: 2026-03-28

---

## 1. 프로젝트 목표와 핵심 로직

**한 줄 요약:** 크림(KREAM) 인기상품을 무신사에서 더 싸게 사서, 크림에서 리셀하여 차익을 얻는 자동화 시스템.

### 핵심 파이프라인

```
크림 인기상품 수집 (최대 100개) → 모델번호로 무신사 3단계 검색
→ 모델번호 정확 매칭 확인 → 사이즈별 가격 비교 (최저가 선택)
→ 수수료 차감 후 순수익 계산 → ROI 기준 충족 시 디스코드 알림
→ 매칭 애매한 건은 #수정 채널에 검토 기록
```

### 2가지 스캔 방식

| 방식 | 흐름 | 트리거 |
|------|------|--------|
| **키워드 스캔** | 무신사 키워드 검색 → 모델번호 추출 → 크림 매칭 | `!스캔`, 30분 주기 자동 |
| **자동스캔 (역방향)** | 크림 인기상품 수집 → 무신사 매칭 → 2단계 ROI 분석 | `!자동스캔`, `!자동스캔시작` |

### 2단계 ROI 분석 (자동스캔)

- **확정 수익 (Confirmed):** 크림 즉시판매가(bid) 기준 — 바로 팔 수 있는 확실한 수익
- **예상 수익 (Estimated):** 최근 체결가 기준 — 시장 시세 기반 예상 수익

### 무신사 3단계 검색 전략

자동스캔 시 무신사에서 상품을 찾는 순서:

1. **1차 — 모델번호 검색:** 복합 모델번호(`315122-111/CW2288-111`)는 슬래시로 분리하여 각각 검색. 하이픈→공백 변형도 시도.
2. **2차 — 상품명 검색:** 모델번호 검색 실패 시 크림 상품명(영문)으로 검색 (최대 4단어).
3. **3차 — 브랜드+상품명 검색:** 브랜드명과 상품명 첫 단어를 조합하여 검색.

**매칭 규칙:**
- 모델번호가 정확히 일치하는 경우만 매칭 인정 (부분 일치 절대 불허)
- 검색 결과 여러 개면 가격이 가장 낮은 상품 선택
- 상품명은 유사하지만 모델번호가 다른 건은 `#수정` 채널에 검토 기록

---

## 2. 폴더/파일 구조

```
크림봇/
├── main.py                           # 봇 엔트리포인트
├── pyproject.toml                    # 의존성 + 프로젝트 메타데이터
├── .env                              # 환경변수 (비밀키 포함, git 미추적)
├── .env.example                      # 환경변수 템플릿
├── PRD_description.md                # 기획 요구사항 문서
├── HANDOFF.md                        # 이 문서
│
├── src/
│   ├── config.py                     # Pydantic 기반 설정 관리
│   ├── scanner.py                    # 스캔 오케스트레이터 (핵심 파이프라인)
│   ├── scheduler.py                  # 주기적 작업 스케줄러 (discord.ext.tasks)
│   ├── matcher.py                    # 모델번호 정규화 + 매칭 로직
│   ├── profit_calculator.py          # 수수료 계산 + ROI 분석 + 시그널 판정
│   ├── price_tracker.py              # 가격 변동 감지
│   │
│   ├── crawlers/
│   │   ├── kream.py                  # 크림 크롤러 (Nuxt __NUXT_DATA__ 파싱)
│   │   ├── musinsa.py                # 무신사 크롤러 (Playwright 기반)
│   │   └── chrome_cdp.py             # Chrome DevTools Protocol 매니저
│   │
│   ├── discord_bot/
│   │   ├── bot.py                    # KreamBot 클래스 + 모든 디스코드 명령어
│   │   └── formatter.py             # 디스코드 Embed 포매팅
│   │
│   ├── models/
│   │   ├── product.py                # 데이터 모델 (KreamProduct, RetailProduct 등)
│   │   └── database.py              # SQLite 비동기 DB 관리
│   │
│   └── utils/
│       ├── logging.py                # 로깅 설정
│       └── resilience.py            # 재시도 데코레이터, Chrome 헬스체크
│
├── tests/
│   ├── test_scanner.py               # 스캐너 테스트
│   ├── test_matcher.py               # 모델번호 매칭 테스트
│   ├── test_profit_calculator.py     # 수수료/수익 계산 테스트
│   ├── test_formatter.py            # 임베드 포매팅 테스트
│   ├── test_database.py              # DB 테스트
│   ├── test_price_tracker.py         # 가격변동 감지 테스트
│   ├── test_scheduler.py             # 스케줄러 테스트
│   ├── test_kream_nuxt.py            # 크림 Nuxt 파싱 테스트
│   ├── test_kream_login_and_options.py # 크림 로그인/옵션 테스트
│   ├── test_auto_scan_live.py        # 실전 E2E 테스트 (네트워크 필요)
│   └── __init__.py
│
├── scripts/
│   └── test_cdp_connection.py        # CDP 연결 테스트 스크립트
│
├── data/
│   ├── kream_bot.db                  # SQLite 데이터베이스
│   └── musinsa_session.json          # 무신사 로그인 세션 파일
│
└── logs/                             # 애플리케이션 로그
```

### 각 파일의 핵심 역할

| 파일 | 역할 |
|------|------|
| `scanner.py` | 전체 스캔 파이프라인 조율. `scan_keyword()`, `auto_scan()`, `_search_musinsa_for_model()` (3단계 검색) |
| `profit_calculator.py` | 크림 수수료 계산, 사이즈별 순수익/ROI 산출, 시그널 판정 |
| `matcher.py` | 모델번호 정규화(`DQ8423-100` ↔ `dq8423 100`) + 크림-무신사 매칭 |
| `scheduler.py` | 30분 주기 자동스캔, 10분 주기 추적스캔, 일일 리포트, Chrome 헬스체크 |
| `kream.py` | 크림 Nuxt SSR 데이터 파싱. 인기상품/상세/시세/거래량 수집 |
| `musinsa.py` | Playwright로 무신사 크롤링. 검색/상세/사이즈별 가격/재고 수집 |
| `chrome_cdp.py` | Windows Chrome을 CDP로 제어 (크림이 Playwright 직접 접속 차단하므로) |
| `bot.py` | 디스코드 봇 명령어 처리 + 알림 발송 + 매칭 검토 채널 기록 |
| `formatter.py` | 수익 알림, 가격변동, 일일리포트 등의 디스코드 Embed 생성 |
| `product.py` | `KreamProduct`, `RetailProduct`, `ProfitOpportunity` 등 데이터클래스 |
| `database.py` | 비동기 SQLite CRUD. 가격 이력, 알림 중복방지, 키워드 관리 |
| `price_tracker.py` | 이전 가격 대비 변동 감지 (1,000원 이상 또는 1% 이상 변동) |
| `resilience.py` | `@retry` 데코레이터, `ChromeHealthChecker` (3회 실패시 자동 재시작) |

---

## 3. 현재 완성된 기능 목록

### 크롤러
- [x] **크림 크롤러** — Nuxt `__NUXT_DATA__` 파싱 (API/메타태그 폴백 포함)
- [x] **크림 인기상품 수집** — 인기순(50)/판매순(40)/급상승(30) 3가지 소스, 최대 100개
- [x] **크림 상세 조회** — 사이즈별 시세(즉시판매가/즉시구매가/최근거래가/입찰수)
- [x] **크림 거래량** — 7일/30일 거래량 + 가격 추세
- [x] **무신사 크롤러** — Playwright 기반 검색/상세/사이즈 크롤링
- [x] **무신사 회원가** — 로그인 세션으로 회원 할인가 수집
- [x] **무신사 품절 필터링** — 재입고 알림/품절/sold out 사이즈 자동 제외
- [x] **Chrome CDP** — Windows Chrome 원격 디버깅 연결

### 분석 엔진
- [x] **수수료 계산** — 크림 판매수수료 + 검수비 + 배송비 정확 계산
- [x] **사이즈별 수익 분석** — 각 사이즈의 순수익/ROI 개별 산출
- [x] **2단계 ROI 분석** — 확정수익(bid 기준) + 예상수익(체결가 기준)
- [x] **시그널 판정** — STRONG_BUY / BUY / WATCH / NOT_RECOMMENDED
- [x] **모델번호 매칭** — 정규화 + 정확 매칭 (퍼지 매칭 없음, 부분 일치 불허)
- [x] **가격 변동 감지** — 이전 가격 대비 유의미한 변동 추적

### 무신사 매칭 (3단계 검색)
- [x] **복합 모델번호 분리 검색** — `315122-111/CW2288-111` → 각각 검색
- [x] **상품명 폴백 검색** — 모델번호 실패 시 크림 상품명으로 2차 검색
- [x] **브랜드+상품명 조합 검색** — 3차 검색
- [x] **최저가 선택** — 매칭 결과 여러 개면 가격이 가장 낮은 상품 자동 선택
- [x] **매칭 검토 채널** — 애매한 건(상품명 유사, 모델번호 불일치)을 #수정 채널에 기록

### 자동화
- [x] **자동스캔 루프** — 30분 주기 크림→무신사 역방향 스캔
- [x] **키워드 스캔** — 30분 주기 무신사→크림 정방향 스캔
- [x] **수익 상품 집중추적** — 수익 상품 발견 시 2시간 동안 10분 주기 재조회
- [x] **일일 리포트** — 매일 자정 Top 5 기회 요약
- [x] **Chrome 헬스체크** — 5분 주기 연결 확인 + 자동 재시작

### 디스코드 봇
- [x] **수익 알림** — 채널별 알림 (매수알림 / 가격변동 / 일일리포트)
- [x] **알림 중복방지** — 상품별 1시간 쿨다운
- [x] **명령어 체계** — 스캔/비교/조회/키워드 관리/설정 등 16개 명령어
- [x] **리치 임베드** — 시그널별 색상, 사이즈 테이블, 수수료 명세
- [x] **매칭 검토 기록** — 모델번호 불일치 건을 #수정 채널에 자동 기록

### 데이터 관리
- [x] **SQLite DB** — 가격 이력, 알림 기록, 키워드 관리
- [x] **세션 영속화** — 무신사 로그인 세션 JSON 저장/복원
- [x] **캐싱** — 모델번호→크림상품 매핑 60분 TTL 캐시

---

## 4. 알려진 문제점

### CDP 로그인 불안정
- 크림이 Playwright 직접 접속을 차단하여 Chrome CDP 사용
- Windows Chrome 프로세스가 비정상 종료 시 포트 9222 충돌
- 수동 1회 로그인 필요 (자동 로그인 미구현)
- `USE_CDP_LOGIN=false`일 때는 pinia 전용 모드 (비로그인, 일부 데이터 제한)

### 크림 데이터 한계
- 비로그인 상태에서 일부 사이즈 시세가 누락될 수 있음
- Nuxt `__NUXT_DATA__` 포맷은 크림 업데이트 시 깨질 수 있음 (폴백 존재하나 불완전)
- 거래량이 실제보다 적게 잡히는 경우 있음 (pinia 데이터 한계)

### 무신사 크롤링
- Playwright 브라우저 메모리 누수 가능 (장시간 운영 시)
- 세션 만료 시 재로그인 필요 (수동)
- 일부 상품의 사이즈 옵션이 비표준 UI로 파싱 실패 가능

### 인기상품 편향
- 크림 인기상품이 에어포스1, 덩크 등 범용 모델에 편중
- 이런 모델은 무신사에서 잘 안 팔아 매칭률이 낮음
- 카테고리 다변화나 급상승 상품 가중치 조정 필요

### 로그 채널 안전장치
- `CHANNEL_LOG`와 `CHANNEL_PROFIT_ALERT`가 같으면 로그 메시지 전송 차단 (매수-알림 채널 보호)

---

## 5. 다음에 해야 할 작업

### 우선순위 높음
1. **모델번호 매핑 테이블** — 크림↔무신사 간 모델번호가 다른 경우를 위한 수동/자동 매핑 DB
2. **무신사 외 리테일 크롤러 추가**
   - 카시나 (kasina.co.kr)
   - 나이키 코리아 (nike.com/kr)
   - 29CM
   - ABC마트
3. **자동 로그인** — 크림/무신사 세션 만료 시 자동 재로그인
4. **매칭 검토 채널 피드백 반영** — #수정 채널에 기록된 건을 수동 매핑으로 전환하는 명령어

### 우선순위 중간
5. **GitHub 연동** — 코드 원격 저장소 + CI/CD
6. **카테고리 확장** — 스니커즈 외 의류/액세서리/테크
7. **알림 필터 커스터마이징** — 사용자별 브랜드/사이즈/ROI 필터
8. **대시보드 웹 UI** — 실시간 모니터링 웹 인터페이스

### 우선순위 낮음
9. **멀티 계정** — 여러 크림/무신사 계정 순환 사용 (레이트리밋 분산)
10. **가격 예측** — 가격 추세 기반 매수 타이밍 추천
11. **Docker 컨테이너화** — 배포 자동화
12. **텔레그램 봇** — 디스코드 외 알림 채널

---

## 6. 환경변수 (.env) 전체 설명

```bash
# ─── Discord ───
DISCORD_TOKEN=               # 디스코드 봇 토큰 (필수)
CHANNEL_PROFIT_ALERT=        # 매수 알림 채널 ID (수익 기회 발견 시)
CHANNEL_PRICE_CHANGE=        # 가격 변동 알림 채널 ID
CHANNEL_DAILY_REPORT=        # 일일 리포트 채널 ID (매일 자정)
CHANNEL_MANUAL_SEARCH=       # 수동 명령어 결과 채널 ID
CHANNEL_SETTINGS=            # 설정 관련 채널 ID
CHANNEL_LOG=                 # 시스템 로그 채널 ID (봇 상태, 에러 등)
CHANNEL_MATCH_REVIEW=        # 매칭 검토용 채널 (#수정, 모델번호 불일치 건 기록)

# ─── Chrome CDP ───
CHROME_PATH=/mnt/c/Program Files/Google/Chrome/Application/chrome.exe
                             # Windows Chrome 실행 경로 (WSL에서 접근)
CHROME_DEBUG_PORT=9222       # Chrome 원격 디버깅 포트
CHROME_USER_DATA_DIR=        # Chrome 디버그 프로필 디렉토리

# ─── 크림 계정 ───
KREAM_EMAIL=                 # 크림 로그인 이메일
KREAM_PASSWORD=              # 크림 로그인 비밀번호
USE_CDP_LOGIN=false          # CDP 로그인 사용 여부 (false면 pinia 전용 모드)

# ─── 무신사 계정 ───
MUSINSA_EMAIL=               # 무신사 로그인 이메일
MUSINSA_PASSWORD=            # 무신사 로그인 비밀번호

# ─── 수수료 (선택, 기본값 있음) ───
SHIPPING_COST_TO_KREAM=3000           # 크림으로 보내는 택배비 (원)
KREAM_FEE_SELL_FEE_RATE=0.06          # 판매 수수료율 (6%)
KREAM_FEE_SELL_FEE_VAT_RATE=0.1       # 수수료 부가세율 (10%)
KREAM_FEE_INSPECTION_FEE=2500         # 검수비 (원)
KREAM_FEE_KREAM_SHIPPING_FEE=3500     # 크림 배송비 (원)

# ─── 자동스캔 (선택, 기본값 있음) ───
AUTO_SCAN_INTERVAL_MINUTES=30         # 자동스캔 주기 (분)
AUTO_SCAN_CONFIRMED_ROI=5.0           # 확정 수익 ROI 알림 기준 (%)
AUTO_SCAN_ESTIMATED_ROI=10.0          # 예상 수익 ROI 알림 기준 (%)
AUTO_SCAN_MAX_PRODUCTS=100            # 1회 스캔 최대 상품 수
AUTO_SCAN_CONCURRENCY=3               # 동시 요청 수
AUTO_SCAN_CACHE_MINUTES=60            # 상품 캐시 유효시간 (분)
```

---

## 7. 실행 방법

### 초기 설정

```bash
# 1. 가상환경 생성 + 의존성 설치
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 2. Playwright 브라우저 설치
playwright install chromium

# 3. .env 파일 생성
cp .env.example .env
# → DISCORD_TOKEN, 채널 ID 등 입력
```

### 봇 실행

```bash
# 디스코드 봇 시작 (모든 스케줄러 포함)
python main.py
```

### 테스트 실행

```bash
# 단위 테스트 전체 실행 (163개)
pytest tests/ -v

# 특정 테스트만
pytest tests/test_profit_calculator.py -v
pytest tests/test_matcher.py -v

# 실전 E2E 테스트 (네트워크 필요, 크림/무신사 실제 접속)
PYTHONPATH=. python tests/test_auto_scan_live.py

# 자동스캔 5개만 빠르게 테스트
MAX_AUTO_SCAN_PRODUCTS=5 PYTHONPATH=. python tests/test_auto_scan_live.py
```

### Claude Code 실행

```bash
# Claude Code로 프로젝트 작업
claude
# 또는 특정 작업
claude "무신사 크롤러에서 복합 모델번호 분리 검색 기능 추가해줘"
```

---

## 8. 기술 스택

| 분류 | 기술 | 버전 | 용도 |
|------|------|------|------|
| 언어 | Python | 3.12+ | 전체 |
| 봇 프레임워크 | discord.py | 2.4.0 | 디스코드 봇 + 주기적 태스크 |
| 브라우저 자동화 | Playwright | 1.49.0 | 무신사 크롤링 |
| HTTP 클라이언트 | aiohttp | 3.9.0 | 크림 API 요청 |
| 데이터베이스 | aiosqlite | 0.20.0 | 비동기 SQLite |
| 설정 관리 | pydantic-settings | 2.7.0 | 환경변수 → 타입 안전 설정 |
| 데이터 모델 | pydantic | 2.10.0 | 데이터 검증 |
| 환경변수 | python-dotenv | 1.0.1 | .env 파일 로딩 |
| 테스트 | pytest + pytest-asyncio | 8.0+ / 0.24+ | 비동기 테스트 |
| 린터 | ruff | 0.8.0 | 코드 품질 |

**런타임 요구사항:**
- WSL 또는 Linux 환경
- Windows Chrome (CDP 모드용, 크림 로그인에 필요)
- 디스코드 봇 토큰 + 서버 채널

---

## 9. 크림 수수료 구조

크림에서 상품을 판매할 때 발생하는 모든 비용:

```
판매가 (크림 즉시판매가 또는 체결가)
├── 판매 수수료:  판매가 × 6%
├── 수수료 부가세: 판매 수수료 × 10%  (= 판매가 × 0.66%)
├── 검수비:       2,500원 (고정)
├── 크림 배송비:   3,500원 (고정, 크림→구매자)
└── 택배비:       3,000원 (고정, 판매자→크림)

총 수수료 = 판매가 × 6.6% + 9,000원
순수익 = 판매가 - 구매가(무신사) - 총 수수료
```

### 계산 예시

```
무신사 구매가: 100,000원
크림 판매가:  130,000원

판매 수수료:  130,000 × 6% = 7,800원
수수료 부가세: 7,800 × 10% = 780원
검수비:       2,500원
크림 배송비:   3,500원
택배비:       3,000원
──────────────
총 수수료:    17,580원
순수익:       130,000 - 100,000 - 17,580 = 12,420원
ROI:          12,420 / 100,000 × 100 = 12.42%
```

---

## 10. 수익 계산 로직

### 자동스캔 2단계 ROI

| 단계 | 기준 가격 | 의미 | 알림 기준 |
|------|-----------|------|-----------|
| **확정 수익** | 크림 즉시판매가 (bid) | 지금 바로 팔 수 있는 가격 | ROI >= **5%** |
| **예상 수익** | 최근 7일 체결가 | 시장 시세 기반 예상 | ROI >= **10%** |

### 시그널 판정 (키워드 스캔)

| 시그널 | 조건 | 임베드 색상 |
|--------|------|------------|
| **STRONG_BUY** | 순수익 >= 30,000원 AND 7일 거래량 >= 10 | 빨강 (0xFF0000) |
| **BUY** | 순수익 >= 15,000원 AND 7일 거래량 >= 5 | 주황 (0xFF8C00) |
| **WATCH** | 순수익 >= 5,000원 | 노랑 (0xFFD700) |
| **NOT_RECOMMENDED** | 위 조건 미충족 OR 7일 거래량 < 3 | 회색 (0x808080) |

### 사이즈 정규화

서로 다른 표기를 통일하여 매칭:
- `270`, `270mm`, `27cm`, `27` → 모두 `270`으로 정규화
- `S`, `M`, `L` 등 문자 사이즈는 그대로 유지

---

## 11. 디스코드 명령어 목록

모든 명령어는 `!` 접두사 사용 (예: `!자동스캔`).

### 스캔 명령어

| 명령어 | 설명 |
|--------|------|
| `!스캔` | 등록된 모든 키워드로 즉시 스캔 실행 |
| `!자동스캔` | 크림 인기상품 기반 1회 자동스캔 |
| `!자동스캔시작` | 30분 주기 자동스캔 루프 시작 |
| `!자동스캔중지` | 자동스캔 루프 중지 |

### 조회 명령어

| 명령어 | 설명 |
|--------|------|
| `!크림 <상품ID>` | 크림 상품 상세 정보 조회 |
| `!비교 <모델번호>` | 크림-무신사 사이즈별 가격 비교 |
| `!무신사 <키워드>` | 무신사 상품 검색 |

### 키워드 관리

| 명령어 | 설명 |
|--------|------|
| `!키워드` | 등록된 모니터링 키워드 목록 |
| `!키워드추가 <키워드>` | 모니터링 키워드 추가 |
| `!키워드삭제 <키워드>` | 모니터링 키워드 삭제 |

### 시스템 명령어

| 명령어 | 설명 |
|--------|------|
| `!설정` | 현재 수수료/시그널/스캔 설정 표시 |
| `!상태` | 봇 상태 (Chrome/크림/무신사 연결, 추적 상품 수, 업타임) |
| `!리포트` | 수동 일일 리포트 생성 |
| `!스케줄러시작` | 모든 주기적 스캔 시작 |
| `!스케줄러중지` | 모든 주기적 스캔 중지 |
| `!추적목록` | 수익 상품 집중추적 목록 (2시간 TTL) |
| `!크롬상태` | Chrome 헬스체크 + 복구 이력 |
| `!도움` | 전체 명령어 도움말 |

---

## 12. 테스트 결과 요약

### 단위 테스트

```
163 tests collected
163 passed
```

| 테스트 파일 | 커버리지 영역 |
|-------------|---------------|
| test_profit_calculator.py | 수수료 계산, ROI 산출, 시그널 판정 |
| test_matcher.py | 모델번호 정규화, 매칭 로직 |
| test_scanner.py | 스캔 파이프라인, 결과 집계 |
| test_formatter.py | 디스코드 임베드 생성, 엣지케이스 |
| test_database.py | DB CRUD, 스키마 |
| test_price_tracker.py | 가격 변동 감지, 임계값 |
| test_scheduler.py | 태스크 라이프사이클 |
| test_kream_nuxt.py | Nuxt __NUXT_DATA__ 파싱 |
| test_kream_login_and_options.py | 크림 로그인 + 마켓 데이터 |

### 실전 E2E 테스트 (2026-03-28 실행)

```
크림 인기상품 수집:  5건 (에어포스1 화이트/블랙, 플랙스, 마인드001, 코비5)
크림 상세 조회:     5건 (사이즈/가격/거래량 정상 수집)
무신사 매칭:        0건 (인기상품이 무신사 미판매 모델)
수익 기회:          0건
```

**매칭 실패 원인:** 크림 인기 스니커즈(에어포스1 등)는 무신사에서 판매하지 않는 모델. 3단계 검색 전략으로 복합 모델번호 분리 검색, 상품명 검색, 브랜드+상품명 검색을 모두 시도했으나 해당 모델이 무신사에 없음. 무신사에서 판매 중인 모델을 대상으로 하면 매칭+수익분석이 정상 동작함.

---

## 부록: 데이터베이스 스키마

```sql
-- 크림 상품 마스터
kream_products (product_id PK, name, model_number, brand, image_url, url)

-- 크림 가격 이력
kream_price_history (product_id, size, sell_now, buy_now, bid_count, last_sale, timestamp)

-- 크림 거래량 이력
kream_volume_history (product_id, volume_7d, volume_30d, price_trend, timestamp)

-- 리테일 상품 마스터
retail_products (source, product_id, name, model_number, brand, url)

-- 리테일 가격 이력
retail_price_history (source, product_id, size, price, original_price, in_stock, timestamp)

-- 알림 이력 (중복방지용, 1시간 쿨다운)
alert_history (kream_product_id, alert_type, best_profit, signal, message_id, timestamp)

-- 모니터링 키워드
monitor_keywords (keyword PK, active, created_at)

-- 봇 런타임 설정
bot_settings (key PK, value)
```

---

## 부록: 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────┐
│                    Discord Bot                       │
│  !자동스캔 / !스캔 / !비교 / !크림 / !무신사 ...       │
│  + #수정 채널 (매칭 검토 기록)                         │
└────────────┬────────────────────────┬────────────────┘
             │                        │
     ┌───────▼───────┐       ┌───────▼───────┐
     │   Scheduler    │       │   Formatter   │
     │ 30m/10m/daily  │       │   Embeds      │
     └───────┬───────┘       └───────────────┘
             │
     ┌───────▼───────┐
     │    Scanner     │  ← 핵심 오케스트레이터
     │  3단계 검색     │     (모델번호→상품명→브랜드+상품명)
     │  최저가 선택    │     (매칭 여러개 → min price)
     └──┬─────────┬──┘
        │         │
┌───────▼──┐  ┌──▼────────┐
│  KREAM   │  │  Musinsa  │
│ Crawler  │  │  Crawler  │
│ (aiohttp)│  │(Playwright│
│ 100개 수집│  │ 5건 조회)  │
└───────┬──┘  └──┬────────┘
        │         │
        ▼         ▼
  ┌─────────────────────┐
  │  Matcher + Profit   │
  │   Calculator        │
  │  (정확 매칭만 인정)   │
  └─────────┬───────────┘
            │
    ┌───────▼───────┐
    │   SQLite DB   │
    │ + Price Track │
    └───────────────┘
```
