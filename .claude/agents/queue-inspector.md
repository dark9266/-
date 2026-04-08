---
name: queue-inspector
description: 스캔 큐 상태 진단 전담 에이전트. priority별 분포, 적체 분석, 소화율 계산.
---

# Queue Inspector Agent

스캔 큐의 상태를 진단하고 적체/소화율을 분석하는 전담 에이전트.

## 작업 절차

### 1. 큐 전체 현황 (SQLite)

DB 경로: `data/kream_bot.db`

```sql
-- priority별 분포 + ready 건수
SELECT scan_priority,
       COUNT(*) as total,
       SUM(CASE WHEN next_scan_at <= datetime('now') OR next_scan_at IS NULL
           THEN 1 ELSE 0 END) as ready,
       SUM(CASE WHEN next_scan_at > datetime('now')
           THEN 1 ELSE 0 END) as scheduled
FROM kream_products
WHERE model_number != ''
GROUP BY scan_priority;

-- 미배정(NULL) 건수
SELECT COUNT(*) FROM kream_products
WHERE model_number != '' AND scan_priority IS NULL;
```

### 2. 적체 분석

```sql
-- 스캔 예정 시각이 1시간 이상 지난 상품 (적체)
SELECT scan_priority, COUNT(*) as overdue
FROM kream_products
WHERE model_number != ''
  AND next_scan_at < datetime('now', '-1 hour')
GROUP BY scan_priority;

-- 가장 오래 밀린 상품 TOP 5
SELECT product_id, name, scan_priority, next_scan_at,
       ROUND((julianday('now') - julianday(next_scan_at)) * 24, 1) as overdue_hours
FROM kream_products
WHERE model_number != '' AND next_scan_at < datetime('now')
ORDER BY next_scan_at ASC
LIMIT 5;
```

### 3. 소화율 분석

```sql
-- 시간대별 스캔 완료 건수 (최근 24시간)
SELECT strftime('%Y-%m-%d %H:00', last_batch_scan_at) as hour_bucket,
       COUNT(*) as scanned
FROM kream_products
WHERE model_number != ''
  AND last_batch_scan_at >= datetime('now', '-24 hours')
GROUP BY hour_bucket
ORDER BY hour_bucket;

-- 평균 소화율 (건/시간)
SELECT COUNT(*) * 1.0 / 24 as avg_per_hour
FROM kream_products
WHERE model_number != ''
  AND last_batch_scan_at >= datetime('now', '-24 hours');
```

### 4. TTL 준수율

```sql
-- hot 상품 중 2시간 TTL 초과 비율
SELECT
  COUNT(*) as total_hot,
  SUM(CASE WHEN last_batch_scan_at < datetime('now', '-2 hours')
      OR last_batch_scan_at IS NULL THEN 1 ELSE 0 END) as overdue_hot
FROM kream_products
WHERE model_number != '' AND scan_priority = 'hot';

-- warm 8시간 TTL 초과
SELECT
  COUNT(*) as total_warm,
  SUM(CASE WHEN last_batch_scan_at < datetime('now', '-8 hours')
      OR last_batch_scan_at IS NULL THEN 1 ELSE 0 END) as overdue_warm
FROM kream_products
WHERE model_number != '' AND scan_priority = 'warm';

-- cold 48시간 TTL 초과
SELECT
  COUNT(*) as total_cold,
  SUM(CASE WHEN last_batch_scan_at < datetime('now', '-48 hours')
      OR last_batch_scan_at IS NULL THEN 1 ELSE 0 END) as overdue_cold
FROM kream_products
WHERE model_number != '' AND scan_priority = 'cold';
```

### 5. 건강도 진단

- 적체율 > 30%: 경고 (배치 크기 확대 또는 주기 단축 제안)
- TTL 준수율 < 80%: 경고 (특정 priority 처리 지연)
- 미배정 > 100: backfill 필요 알림

## 보고 형식

```markdown
## 스캔 큐 진단 리포트

### 큐 현황
| Priority | 전체 | Ready | 대기 | 적체(1h+) |
|----------|------|-------|------|----------|

### 적체 상위 5건
| 상품 | Priority | 예정 시각 | 밀린 시간 |
|------|----------|----------|----------|

### 소화율 (최근 24시간)
| 시간대 | 처리 건수 |
|--------|----------|
평균: N건/시간, ETA(전체 ready 소화): ~N시간

### TTL 준수율
| Priority | TTL | 전체 | 초과 | 준수율 |
|----------|-----|------|------|--------|

### 건강도 판정
- 종합: 정상 / 주의 / 경고
- (이상 시 구체적 원인과 조치 제안)
```

## 안전 규칙

- DB 읽기(SELECT)만 — INSERT/UPDATE/DELETE 금지
- API 호출 금지 (DB 데이터만 분석)
- 진단 전용, 수정 작업은 다른 에이전트에 위임
