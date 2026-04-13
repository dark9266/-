# Phase 4 설계 메모 — 지능 필터 + 피드백 루프

Phase 3 어댑터 양산 + 델타 엔진 + 파일럿 배선 완료 후 진입.
케찹매니아가 **원리적으로 못 하는** 영역. 구조적 해자.

## 축 7 — 지능 필터 (`signal_scorer`)

### 목적
알림 전 단계에서 "수익 나는" 이상의 신호 품질 점수화 → 거짓 알림 추가 차단.

### 입력
`ProfitFound` 이벤트 + 크림 거래 내역 API (`/api/p/e/products/{id}/sales`).

### 점수 축 (초안)
| 축 | 계산 | 근거 |
|----|------|------|
| 유동성 깊이 | 30일 거래량 / 현재 매물 수 | 1 초과 = 수요 > 공급 |
| 변동성 | 7일 가격 std dev / 평균 | 낮을수록 안정 체결 기대 |
| 체결 속도 | 최근 5건 거래 간격 중앙값 | 짧을수록 즉시판매 가능성 ↑ |
| 가격 위치 | 현재 sell_now vs 30일 P25~P75 | 중간 이하면 진입 여유 |

### 구현 위치
- `src/core/signal_scorer.py` — 순수 함수 모듈 (DB/HTTP 없음)
- `src/adapters/kream_sales_client.py` — 거래 내역 조회 (기존 KreamCrawler 재사용)
- Orchestrator 확장: `ProfitFound → (scorer) → ScoredProfit → AlertSent`
- 점수 임계값은 config 로 주입 (초기 하드코딩 → Phase 4.2 에서 피드백 루프로 자동 튜닝)

### 리스크
- 크림 거래 내역 API 호출 증가 → 하드 캡 재계산 필수
- 점수 가중치 주관 — 초기엔 단순 합산, 피드백 루프로 학습

## 축 8 — 피드백 루프 (`alert_outcome_tracker`)

### 핵심 차별
케찹은 알림 후 사용자가 앱 **밖** 에서 구매 → 체결 관측 불가.
우리는 Discord 푸시 + 크림 체결 내역 API → **알림 후 N분 내 실제 체결 여부** 검증 가능.

### 스키마 (이미 Phase 0 에 자리 잡힘)
```sql
CREATE TABLE alert_followup (
    alert_id INTEGER PRIMARY KEY,
    fired_at REAL,
    kream_product_id INTEGER,
    size TEXT,
    retail_price INTEGER,
    kream_sell_price_at_fire INTEGER,
    checked_at REAL,
    actual_sold INTEGER DEFAULT 0,
    actual_price INTEGER
);
```

### 루프
1. `AlertSent` 발생 → `alert_followup` INSERT (fired_at 세팅)
2. N분 후 (초기 30분) `alert_followup` 미체크 행 pick
3. 크림 거래 내역 조회 → size 일치 체결 있는가?
   - O → `actual_sold=1`, `actual_price=체결가`
   - X → `actual_sold=0`
4. 7일마다 집계: 점수 축별 hit rate → `signal_scorer` 가중치 gradient 수정

### 구현 위치
- `src/core/alert_outcome_tracker.py` — Orchestrator 구독자 + 주기 sweep
- `src/core/scorer_tuner.py` — 7일 집계 + 가중치 업데이트 (간단한 online learning)

### 리스크
- 체결 관측 지연(30분) → 실시간 튜닝 아님 (Phase 4.2)
- size 매칭 모호 → 다중 사이즈 알림은 가장 빠른 체결만 정답 처리

## Phase 4 에이전트

### `signal-scorer` (JIT 생성)
- 트리거: Phase 4 진입
- 역할: 축 7 scorer 모듈 구현 + 거래 내역 API 통합 + 점수 임계값 실측 기반 초기값 설정
- 권한: Read + Edit/Write + Bash(GET)

### `alert-outcome-tracker` (JIT 생성)
- 트리거: Phase 4 진입
- 역할: 축 8 피드백 루프 구현 — 체결 관측 sweep + 가중치 튜닝 + 일일 집계
- 권한: Read + Edit/Write + Bash(SELECT+GET)

## 전제 조건 (Phase 4 진입 전 충족해야 함)
1. 24h 파일럿 완료 — `v3_alerts.jsonl` 에 실제 알림 샘플 ≥ 50 건
2. `alert_sent` 테이블에 체결 관측용 최소 레코드 축적
3. 크림 호출 실측 캡 확정 (`kream_api_calls` 7일 분포 분석)
4. 파일럿 중 거짓 알림 0 건 (정확도 하드 제약)

## 추정 공수
- 축 7 scorer: 3~5 일 (scorer 모듈 + 통합 + 임계값 튜닝)
- 축 8 outcome tracker: 4~6 일 (sweep + 집계 + gradient)
- 통합 테스트 + 24h 관측: 2~3 일
- **합산 1~2주** (로드맵 Phase 4 구간과 일치)
