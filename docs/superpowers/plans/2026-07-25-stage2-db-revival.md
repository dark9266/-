# 2단계: DB 되살리기 + cold 갱신 전략 — 구현 계획

> **For agentic workers:** 각 조각은 `kream-executor`에 위임해 TDD로 구현. 조각 끝마다 머리가 검증.
> 근거: explorer 정찰(2026-07-25) + 코덱스 S/xhigh 설계 자문(`reports/codex/codex_e340e67ee2182e3419a9.md`).

**Goal:** 3/28 이후 미갱신 상태인 48.5k 크림 DB의 **거래량 증거**를 되살리고(volume 미확인 96.6%),
collect_queue 적체 21.5k를 드레인하며, 지속 가능한 cold 재검사 정책을 심는다. 실계정 안전이 속도보다 우선.

**실측 전제 (재확인 불요):** 크림 방어 = TLS 지문만(safari 지문 통과, 2~3s 간격 실측 안전). 미측정 = 수백~수천
누적 시 IP 임계 → 그래서 점진 램프. 경량 호출: 거래량 = screens API 1회/상품, 시세+거래량 = snapshot_light 1회.

## Global Constraints (불변 — 매 조각 적용)
- 크림 GET 전용. 47k 전체 시세 갱신 금지. 로컬 시세로 수익 계산 금지(알림은 실시간 sell_now).
- **10k 하드캡 = 사고 방지벽이지 일일 목표가 아니다.** 백그라운드는 소프트캡 램프를 따른다.
- 응답 없음/파싱 실패를 "거래량 0"이나 "영구 not_found"로 확정 금지 (1c 상태 분리 원칙과 동일).
- 매칭 = 47k 전체 / 크림 live 조회 = 유동성 우선. 거래량 게이트 1 고정. 재론 금지.
- IP 로테이션·프록시·로그인 추가 금지(이 단계에서). 봇 프로세스 기동은 사장 전용(UI START).

---

## 조각 2-0: 전역 KREAM 페이서 + 백그라운드 예산 브로커 (최우선 — 다른 조각의 토대)

**Files:** Modify `src/core/kream_budget.py`(확장) · Create `src/core/kream_pacer.py` · Test

- **예산 브로커** (`kream_budget` 확장):
  - 하드캡 rolling 24h 10,000 유지 (기존).
  - **라이브 예약분 1,000**: 백그라운드 배치가 못 빌려 씀. 배치 허용량 =
    `min(현 단계 soft cap, 10000 - 최근 24h 전체 사용량 - 1000)`.
  - **백그라운드 소프트캡 램프** (환경변수/DB 상태로 단계 기록): 카나리 100건 → 첫 24h 500 →
    2일차 1,000 → 3~6일차 2,000 → 7일 연속 정상 후에도 이 단계 최대 3,000/rolling 24h.
    단계 전환은 자동 시간경과가 아니라 "이상 없음" 기록 후 수동/스크립트 확인 전환.
- **전역 페이서** (`kream_pacer.py`): 동시성 1, 요청 시작 간격 `2.5s + uniform(0, 1.0)s`.
  모든 호출자·모든 재시도가 같은 페이서를 통과(재시도가 페이서·쿼터를 우회하면 안 됨).
  caller별 sleep 과 이중 딜레이 금지 — 한 계층에서만 제어.
- **서킷브레이커**: 403/429 **1회** → 백그라운드 전면 즉시 중단 + 자동 급속 재개 금지(수동 재개 플래그).
  동일 경로 연속 5xx/timeout 3회 → 해당 배치 중단·냉각. 차단 시 재시도로 예산 소모 금지.
- standalone 배치 실행 중 다른 백그라운드 크림 루프와 겹치지 않게: 페이서 전역 공유가 안 되는
  프로세스 분리 상황이면 배치 실행 중 다른 루프 maintenance-mode 문서화(지금은 봇 정지 상태라 단순).

**검증(TDD):** 예약분 침범 불가 / 램프 단계별 허용량 계산 / 페이서 간격 하한 보장(mock 시계) /
403 1회 → 이후 acquire 전부 거부 + 수동 재개 플래그로만 해제 / 재시도가 쿼터 소비하는지.

---

## 조각 2-1: `bootstrap_cold_volumes_light` 정식화 (.tmp → 정식 + 상태모델)

**Files:** `scripts/bootstrap_cold_volumes_light.py.tmp` → `scripts/bootstrap_cold_volumes_light.py` · DB 마이그레이션(컬럼 추가) · Test

- 대상: retail-matched cold + `last_volume_check IS NULL` (기존 .tmp 의 JOIN 유지). 1호출/상품(screens).
- **결과 상태 분리** (1c 원칙): `success_positive` / `success_zero`(거래영역 정상 파싱으로 "실제 0" 확인) /
  `retryable`(timeout·차단·파싱실패) / `quarantined`(삭제·비공개). `last_volume_check` 는 성공 2종에서만 갱신.
  실패는 `last_volume_attempt_at`·`volume_attempt_count`·`next_volume_attempt_at`·`last_volume_error` 컬럼(신설)에만.
- **실행 상태머신**: 작은 chunk 원자 lease(`lease_owner`/`lease_until`) → succeeded | retry_wait | quarantined.
  at-least-once + idempotent upsert 명시(외부 GET 과 로컬 commit 사이 exactly-once 불가 수용).
- 페이서·브로커(2-0) 경유 필수. `kream_purpose("bootstrap_light")` 태깅 유지.
- dry-run 출력 확장: 고유 대상 수 / rolling-24h 사용량·소프트 잔여·하드 잔여 / 재시도 포함 최선·예상·최악
  호출수 / 페이싱 기준 벽시계 시간 / collect_queue 와의 중복 수.
- 티어 재산정: volume 결과로 `hot ≥5 / warm 1~4 / cold 0 / unknown` 중앙 함수 1곳에서.
  (1~2 를 zero 와 같은 cold 로 묶지 않는다 — 유동성 우선 원칙.)

**검증(TDD):** 상태 4종 분기 / lease 동시실행 방지 / idempotent 재실행 / dry-run 수치 정확성(mock DB) /
페이서 경유 확인. 실호출 0 (라이브는 2-4).

---

## 조각 2-2: collect_queue 드레인 배치 (예산 20%)

**Files:** Create `scripts/drain_collect_queue.py` (collector.collect_pending 재사용/확장) · Test

- 순서: ① 네트워크 0으로 현 `kream_products` 와 정규화 모델번호 재대조(그 사이 등록된 것 소거) ②
  retail 재고/최근 관측 증거 있는 항목 ③ 최근 추가·갱신 항목 ④ 오래된 backlog.
- `canonical_model_key` 로 중복 검색 방지(같은 모델 여러 소싱처 URL = source-reference 로 연결).
- `attempts` 는 "정상 검색 응답에서 exact match 없음"일 때만 증가. transport 실패는 retryable.
- 재검색 간격: 즉시 3연속 금지 — 지금 → 7일 → 30일 → dormant. 새 retail 관측 시 dormant 깨움.
- 예산: 백그라운드 배분의 20% (bootstrap 80%). 첫 500건 표본 후
  `volume≥1 active match 발견수 / 실 호출수` 수율을 bootstrap 수율과 비교해 배분 재조정 근거 기록.

**검증(TDD):** 로컬 재대조 소거 / 순서 정렬 / canonical key dedup / attempts 증가 조건 / 재검색 스케줄. 실호출 0.

---

## 조각 2-3: cold 재검사 정책 (`next_volume_check_at`)

**Files:** Modify 티어/스케줄 중앙 모듈(2-1에서 만든 곳) · Create `scripts/recheck_cold_volumes.py`(또는 bootstrap 스크립트에 모드 추가) · Test

- `last_volume_check ASC` 가 아니라 **`next_volume_check_at`(신설)** 기준:
  volume 0 + active retail match: 30일 / 1~4: 7일 / ≥5: 3~7일 / retail 미연결 zero: 60~90일(이 단계 보류 허용) /
  retail 재고·가격 이벤트 발생 시 예정일 앞당김. due 에 ±10% 결정적 지터(월간 동시 집중 방지).
- 초기 배분 0% — retail-matched bootstrap 이 대부분 끝난 뒤 queue 70% / cold TTL 30% 로 전환.
- 티어 승격은 live 조회 우선순위만 바꾼다 — "전체 시세 주기 갱신"과 연결 금지.

**검증(TDD):** TTL 매트릭스 / 지터 결정성 / 이벤트 앞당김 / due 선별 쿼리. 실호출 0.

---

## 조각 2-4: 라이브 1차 — 카나리 + retail-matched bootstrap 실행

- 카나리 100건 (페이서·브로커 경유, 사장 보고 후) → 이상 없으면 램프 단계대로 진행.
- 매 500~1,000호출마다 `volume≥1 발견 / 실 호출` 수율 재평가 기록. retail 미연결 cold 확장은
  남는 쿼터로만 — 완수 의무 migration 으로 만들지 않는다.
- 실행 결과 검증: DB 재조회로 volume 분포·상태 분포 변화 보고 (2026-04-26 SQL 타입 함정 주의 —
  kream_products.updated_at 등은 TEXT datetime).

---

## 실행 순서
2-0(토대) → 2-1 → 2-2 → 2-3 (전부 실호출 0, TDD) → 2-4 라이브(카나리 → 램프). 각 조각 executor 위임 →
머리 검증 → 커밋. 코어 모듈(kream_budget 등) 수정 조각은 code-reviewer 의무. 2-4 카나리 결과는 사장 보고.

## 미루는 것 (스코프 밖)
- 프록시/IP 로테이션 (미측정 임계 실증 후 별도 판단) · 전체 cold 완수 migration · hourly spike loop 확대 적용 ·
  스케줄러 상시 루프 배선(봇 재가동 = 사장 UI START 이후 별도 조각).
