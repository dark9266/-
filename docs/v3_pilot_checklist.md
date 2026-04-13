# v3 런타임 파일럿 체크리스트

Phase 3 어댑터 양산 완료 후, `V3_RUNTIME_ENABLED=true` 로 실가동 전 확인할 항목.

## 1. 크림 호출 예산 계산 (핵심 — 실계정 보호)

**하드 캡**: `KREAM_DAILY_CAP=10000` (`src/core/kream_budget.py`)
**소프트 스로틀**: `CallThrottle(rate_per_min=15, burst=20)` (`src/core/runtime.py`)

### 예상 호출량 (어댑터 N개 기준)
- 어댑터 주기 덤프: 1회/30분 × 48회/일 = **0회 크림 호출** (소싱처 API만)
- 크림 조회는 `CandidateMatched → ProfitFound` 단계에서만 — 후보당 1회 (sell_now)
- 예상 후보 수: 어댑터 1개당 ~10건/덤프 × 48회 × N개 = **480 × N 건/일**
- hot watcher: 130건 × 60초 폴링 = **187,200건/일** ← ⚠️ 이게 문제

### 조정 필요
- [ ] hot watcher 폴링 주기를 60s → 5min 으로 상향 조정 가능? 
      → 130건 × 288회/일 = **37,440건/일**. 여전히 캡 초과.
- [ ] hot watcher는 변경 감지(델타 엔진) 필요. 전수 폴링 대신 **관심 상품만** 크림 호출.
- [ ] 초기 파일럿에선 hot watcher 비활성화, 푸시 어댑터만 ON 권장

## 2. 알림 중복 방지 (v2 병행 운영)

- [ ] `v3_alert_logger` 가 Discord 발송 안 함 확인 (`grep -n discord src/core/runtime.py src/core/v3_alert_logger.py` → 0 매치)
- [ ] `alert_sent` 테이블 dedup — 같은 checkpoint_id 중복 INSERT 방지 (Phase 2.3 리뷰에서 추가)
- [ ] v3 경로 알림은 `logs/v3_alerts.jsonl` 에만 기록 → 안정화 확인 후 Discord 전환

## 3. 기동 예외 격리

- [ ] `main.py` 에서 `V3Runtime.start()` 가 try/except 로 감싸져 있는가
- [ ] v3 기동 실패해도 v2 봇(scheduler.py 루프 6개)은 정상 기동
- [ ] 시작 10초 내 v3 크래시 시 ENABLED 플래그 자동 false 백업 경로

## 4. 롤백 절차

1. `.env` 에서 `V3_RUNTIME_ENABLED=false` 변경
2. 봇 재시작
3. v2 루프만 돌아가는지 확인
4. `logs/v3_alerts.jsonl` 아카이브 (분석용)

## 5. 24h 파일럿 관측 지표

- [ ] 크림 호출 총량 (`SELECT COUNT(*) FROM kream_api_calls WHERE ts > datetime('now','-1 day')`)
- [ ] 캡 근접도 (1000회 단위 구간별 분포)
- [ ] v3 이벤트 처리량 (`orchestrator.stats()` JSON dump)
- [ ] Phase 2.3 체크포인트 pending/failed 수
- [ ] v3 vs v2 알림 동일성 (둘 다 같은 기회를 잡았는가 — 샘플 10건 수동 비교)

## 6. 파일럿 종료 조건

### 성공 기준
- 24h 내 크림 호출 < 5,000 (캡 50% 이하 유지)
- checkpoint pending/failed = 0
- v3 알림 > 0 건
- v3 기동 실패 0 회

### 실패 기준 → 즉시 롤백
- 크림 호출 캡 90% 이상 도달
- v3 크래시 1회 이상
- 거짓 알림 1건 이상 발견 (정확도 하드 제약)

## 7. 파일럿 이후 단계

- 안정성 확인 → v3 알림 채널 Discord 전환 (기존과 별도 채널 권장)
- 1주 관측 → v2 `tier2_monitor`, `continuous_scanner` 등 단계적 폐기
- 소싱처 N개 이상 안정 → Phase 4 진입 (지능 필터 + 피드백 루프)
