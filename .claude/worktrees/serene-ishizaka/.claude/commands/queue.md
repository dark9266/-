스캔 큐 현황 대시보드 — priority별 분포, 적체, ETA.

아래 정보를 수집하여 스캔 큐 상태를 요약해주세요:

## 수집할 정보 (SQLite 쿼리)

DB 경로: `data/kream_bot.db`
python3으로 sqlite3 모듈을 사용하여 쿼리 실행.

### 1. 우선순위별 분포

```sql
SELECT scan_priority,
       COUNT(*) as total,
       SUM(CASE WHEN next_scan_at <= datetime('now') OR next_scan_at IS NULL
           THEN 1 ELSE 0 END) as ready,
       SUM(CASE WHEN next_scan_at > datetime('now')
           THEN 1 ELSE 0 END) as waiting
FROM kream_products
WHERE model_number != ''
GROUP BY scan_priority
ORDER BY CASE scan_priority WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END;
```

### 2. 스케줄 미배정 (backfill 필요)

```sql
SELECT COUNT(*) as unscheduled
FROM kream_products
WHERE model_number != '' AND next_scan_at IS NULL AND scan_priority IS NULL;
```

### 3. 최근 스캔 완료 (last_batch_scan_at 기준)

```sql
SELECT scan_priority,
       COUNT(*) as scanned_last_hour
FROM kream_products
WHERE model_number != ''
  AND last_batch_scan_at >= datetime('now', '-1 hour')
GROUP BY scan_priority;
```

### 4. 다음 배치 미리보기 (상위 10건)

```sql
SELECT product_id, name, brand, scan_priority, next_scan_at,
       volume_7d, last_batch_scan_at
FROM kream_products
WHERE model_number != ''
  AND (next_scan_at IS NULL OR next_scan_at <= datetime('now'))
ORDER BY
  CASE scan_priority WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END,
  CASE WHEN next_scan_at IS NULL THEN 0 ELSE 1 END,
  next_scan_at ASC
LIMIT 10;
```

### 5. ETA 추정

```sql
-- 배치 크기 50건/5분 기준, ready 건수로 ETA 계산
SELECT
  SUM(CASE WHEN (next_scan_at IS NULL OR next_scan_at <= datetime('now'))
      THEN 1 ELSE 0 END) as total_ready
FROM kream_products
WHERE model_number != '';
```

ETA = total_ready / 50 * 5분

## 출력 형식

```markdown
## 스캔 큐 현황

### 우선순위별 분포
| Priority | 전체 | Ready | 대기중 | 지난 1시간 완료 |
|----------|------|-------|--------|---------------|

### 적체 현황
- 미배정(backfill 필요): N건
- Ready 총 건수: N건
- 예상 소화 시간: ~N시간 (50건/5분 기준)

### 다음 배치 미리보기 (상위 10건)
| # | 상품명 | 브랜드 | Priority | 거래량(7d) | 마지막 스캔 |
|---|--------|--------|----------|-----------|-----------|
```
