# v2 운영 명령 + 에이전트 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLAUDE.md 문서 정합성 업데이트 + /health, /queue, /trace 슬래시 명령 구현 + queue-inspector, scan-debugger 에이전트 구현

**Architecture:** 슬래시 명령은 `.claude/commands/` MD 파일로 구현 (기존 status.md/verify.md 패턴). 에이전트는 `.claude/agents/` MD 파일로 구현 (기존 kream-monitor.md/reverse-scanner.md 패턴). 모두 DB SELECT + GET 전용, 수정 작업 없음.

**Tech Stack:** Markdown (Claude Code commands/agents), SQLite (SELECT only), httpx (GET only)

---

## File Structure

| 파일 | 역할 | 작업 |
|------|------|------|
| `CLAUDE.md` | 프로젝트 문서 | Modify: "(구현 예정)" 표기 제거 |
| `.claude/commands/health.md` | /health 슬래시 명령 | Create |
| `.claude/commands/queue.md` | /queue 슬래시 명령 | Create |
| `.claude/commands/trace.md` | /trace 슬래시 명령 | Create |
| `.claude/agents/queue-inspector.md` | 큐 진단 에이전트 | Create |
| `.claude/agents/scan-debugger.md` | 스캔 추적 에이전트 | Create |

---

### Task 1: CLAUDE.md "(구현 예정)" 표기 업데이트

**Files:**
- Modify: `CLAUDE.md` (lines 28, 37, 54, 128-129, 138-140)

- [ ] **Step 1: 5곳의 "(구현 예정)" / "(v2 예정)" 표기를 현재 구현 상태에 맞게 업데이트**

변경 대상:

1. Line 28 — Tier 0 연속 배치 스캔: `(구현 예정)` → 제거 (이미 구현됨)
```
| **Tier 0** | 5분/50건 | 연속 배치 스캔 — 47k 전체 순환 | `src/continuous_scanner.py` |
```

2. Line 37 — hot/warm/cold 우선순위 큐: `(구현 예정)` → 제거
```
### hot/warm/cold 우선순위 큐
```

3. Line 54 — continuous_scanner.py 설명: `**(구현 예정)**` → 제거
```
- `src/continuous_scanner.py` — next_scan_at 기반 47k 연속 배치 스캔
```

4. Lines 128-129 — 에이전트 표: `**(v2 예정)**` → 제거 (이번 작업에서 구현)
```
| `queue-inspector` | 스캔 큐 상태 진단, 적체/소화율 | Read + Bash(SELECT) |
| `scan-debugger` | 상품별 스캔 파이프라인 추적 | Read + Bash(GET+SELECT) |
```

5. Lines 138-140 — 슬래시 명령 표: `**(v2 예정)**` → 제거 (이번 작업에서 구현)
```
| `/queue` | 스캔 큐 현황 — priority별 분포, 적체, ETA |
| `/trace` | 특정 모델번호 스캔 파이프라인 추적 |
| `/health` | 소싱처 4곳 헬스체크 — 응답시간, 서킷브레이커 |
```

- [ ] **Step 2: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 구현 완료 항목 표기 업데이트 — 구현예정 제거"
```

---

### Task 2: /health 슬래시 명령 구현

**Files:**
- Create: `.claude/commands/health.md`

- [ ] **Step 1: health.md 작성**

```markdown
소싱처 4곳 헬스체크 — 응답시간, 서킷브레이커 상태.

아래 순서로 소싱처 상태를 점검하고 결과를 요약해주세요:

## 1. 서킷브레이커 상태 (코드 분석)

`src/crawlers/registry.py`의 `RETAIL_CRAWLERS` 딕셔너리를 분석:
- 각 소싱처의 `fail_count`, `disabled_until` 확인
- 활성/비활성 상태 판별

현재 등록된 소싱처: 무신사, 29CM, 나이키, 아디다스

## 2. 실시간 응답 테스트 (GET 전용)

각 소싱처에 테스트 검색 요청 1건씩 (타임아웃 10초):

```bash
# 무신사 — 검색 API
python3 -c "
import httpx, time
start = time.time()
r = httpx.get('https://goods-search.musinsa.com/api/v2/search', params={'keyword': 'nike', 'size': 1}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
print(f'무신사: {r.status_code} ({time.time()-start:.2f}s)')
"

# 29CM — 검색 API
python3 -c "
import httpx, time
start = time.time()
r = httpx.get('https://search-api.29cm.co.kr/api/v4/products', params={'keyword': 'nike', 'limit': 1}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
print(f'29CM: {r.status_code} ({time.time()-start:.2f}s)')
"

# 나이키 — 검색 페이지
python3 -c "
import httpx, time
start = time.time()
r = httpx.get('https://www.nike.com/kr/w?q=air+max', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10, follow_redirects=True)
print(f'나이키: {r.status_code} ({time.time()-start:.2f}s)')
"

# 아디다스 — taxonomy API
python3 -c "
import httpx, time
start = time.time()
r = httpx.get('https://www.adidas.co.kr/api/plp/content-engine/search', params={'query': 'ultraboost'}, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.adidas.co.kr/'}, timeout=10)
print(f'아디다스: {r.status_code} ({time.time()-start:.2f}s)')
"
```

## 3. Rate Limit 설정 확인

`src/config.py`에서 소싱처별 rate limit 설정값 조회.

## 출력 형식

```markdown
## 소싱처 헬스체크 리포트

### 서킷브레이커 상태
| 소싱처 | 상태 | 실패횟수 | 비활성화 해제 |
|--------|------|---------|-------------|

### 응답 테스트
| 소싱처 | HTTP 상태 | 응답시간 | 판정 |
|--------|----------|---------|------|

### Rate Limit 설정
| 소싱처 | max_concurrent | min_interval | 시간당 처리량 |
|--------|:-:|:-:|:-:|

### 종합 판정
(각 소싱처별 정상/주의/장애 판정)
```
```

- [ ] **Step 2: 커밋**

```bash
git add .claude/commands/health.md
git commit -m "feat: /health 슬래시 명령 추가 — 소싱처 4곳 헬스체크"
```

---

### Task 3: /queue 슬래시 명령 구현

**Files:**
- Create: `.claude/commands/queue.md`

- [ ] **Step 1: queue.md 작성**

```markdown
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
```

- [ ] **Step 2: 커밋**

```bash
git add .claude/commands/queue.md
git commit -m "feat: /queue 슬래시 명령 추가 — 스캔 큐 현황 대시보드"
```

---

### Task 4: /trace 슬래시 명령 구현

**Files:**
- Create: `.claude/commands/trace.md`

- [ ] **Step 1: trace.md 작성**

```markdown
특정 모델번호 스캔 파이프라인 추적.

사용법: `/trace <모델번호>` (예: `/trace CW2288-111`)

인자로 전달된 모델번호의 전체 파이프라인을 추적합니다.
인자가 없으면 사용자에게 모델번호를 물어보세요.

## 추적 순서

### 1. 크림 DB 조회 (SQLite)

DB 경로: `data/kream_bot.db`

```sql
-- 상품 정보
SELECT product_id, name, brand, model_number, category,
       volume_7d, volume_30d, scan_priority, next_scan_at,
       last_batch_scan_at, refresh_tier, updated_at
FROM kream_products
WHERE model_number LIKE '%{모델번호}%'
ORDER BY volume_7d DESC;

-- 가격 이력 (최근 5건)
SELECT kph.*
FROM kream_price_history kph
JOIN kream_products kp ON kp.product_id = kph.product_id
WHERE kp.model_number LIKE '%{모델번호}%'
ORDER BY kph.created_at DESC
LIMIT 5;

-- 거래량 스냅샷 (최근 5건)
SELECT kvs.*
FROM kream_volume_snapshots kvs
JOIN kream_products kp ON kp.product_id = kvs.product_id
WHERE kp.model_number LIKE '%{모델번호}%'
ORDER BY kvs.snapshot_at DESC
LIMIT 5;

-- 알림 이력
SELECT * FROM alert_history
WHERE model_number LIKE '%{모델번호}%'
ORDER BY created_at DESC
LIMIT 5;
```

### 2. 소싱처 검색 테스트 (GET 전용)

각 소싱처에서 해당 모델번호로 실제 검색 1건씩:

```python
# 무신사
import httpx
r = httpx.get('https://goods-search.musinsa.com/api/v2/search',
    params={'keyword': '{모델번호}', 'size': 5},
    headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
# 결과에서 상품명, 가격, 모델번호 추출

# 29CM
r = httpx.get('https://search-api.29cm.co.kr/api/v4/products',
    params={'keyword': '{모델번호}', 'limit': 5},
    headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)

# 나이키 — 모델번호로 직접 PDP
r = httpx.get(f'https://www.nike.com/kr/t/{모델번호}',
    headers={'User-Agent': 'Mozilla/5.0'}, timeout=10, follow_redirects=True)

# 아디다스 — 모델번호 검색
r = httpx.get('https://www.adidas.co.kr/api/plp/content-engine/search',
    params={'query': '{모델번호}'},
    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.adidas.co.kr/'},
    timeout=10)
```

### 3. 매칭 테스트

`src/matcher.py`의 로직으로 소싱처 결과 ↔ 크림 DB 매칭 시뮬레이션:
- 모델번호 정규화 후 exact match 여부
- 매칭 성공 시 사이즈별 가격 비교

### 4. 수익 시뮬레이션

매칭 성공한 소싱처-크림 쌍에 대해:
- 정산가 = 크림 sell_now - (기본료 2,500 + 크림가 × 6%) × 1.1 - 배송비 3,000
- 순수익 = 정산가 - 소싱처 매입가
- ROI = 순수익 / 매입가 × 100

### 5. 스캔 캐시 확인

`data/scan_cache.json`에서 해당 모델번호의 캐시 상태 확인.

## 출력 형식

```markdown
## 파이프라인 추적: {모델번호}

### 크림 DB
| 항목 | 값 |
|------|---|
| product_id | ... |
| 상품명 | ... |
| 브랜드 | ... |
| 거래량(7d/30d) | .../... |
| 스캔 우선순위 | hot/warm/cold |
| 다음 스캔 | ... |
| 마지막 배치 스캔 | ... |

### 소싱처 검색 결과
| 소싱처 | 검색 결과 | 모델번호 매칭 | 가격 | 사이즈 |
|--------|----------|-------------|------|--------|

### 수익 시뮬레이션
| 소싱처 | 사이즈 | 매입가 | 크림 sell_now | 수수료 | 순수익 | ROI |
|--------|--------|--------|-------------|--------|--------|-----|

### 캐시 상태
- 캐시 존재: yes/no
- 캐시 만료: ...

### 진단
(매칭 실패 원인, 수익 미달 원인, 스캔 스케줄 이상 등)
```
```

- [ ] **Step 2: 커밋**

```bash
git add .claude/commands/trace.md
git commit -m "feat: /trace 슬래시 명령 추가 — 모델번호별 스캔 파이프라인 추적"
```

---

### Task 5: queue-inspector 에이전트 구현

**Files:**
- Create: `.claude/agents/queue-inspector.md`

- [ ] **Step 1: queue-inspector.md 작성**

```markdown
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

-- warm 8시간, cold 48시간도 동일 패턴
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
```

- [ ] **Step 2: 커밋**

```bash
git add .claude/agents/queue-inspector.md
git commit -m "feat: queue-inspector 에이전트 추가 — 스캔 큐 진단/적체 분석"
```

---

### Task 6: scan-debugger 에이전트 구현

**Files:**
- Create: `.claude/agents/scan-debugger.md`

- [ ] **Step 1: scan-debugger.md 작성**

```markdown
---
name: scan-debugger
description: 상품별 스캔 파이프라인 추적 전담 에이전트. 모델번호 기준 전 구간 디버깅.
---

# Scan Debugger Agent

특정 상품의 스캔 파이프라인 전 구간을 추적하여 문제를 진단하는 전담 에이전트.

사용 시 모델번호 또는 product_id를 인자로 전달받습니다.

## 작업 절차

### 1. 상품 식별 (SQLite)

DB 경로: `data/kream_bot.db`

```sql
SELECT product_id, name, brand, model_number, category,
       volume_7d, volume_30d, scan_priority, next_scan_at,
       last_batch_scan_at, refresh_tier, updated_at,
       sell_now_min, sell_now_max
FROM kream_products
WHERE model_number LIKE '%{인자}%' OR product_id = '{인자}'
ORDER BY volume_7d DESC;
```

여러 건 매칭 시 volume_7d 최대인 상품 선택 (사용자 확인).

### 2. 크림 시세 확인 (DB)

```sql
SELECT size, sell_now, buy_now, last_sale_price
FROM kream_price_history
WHERE product_id = '{product_id}'
ORDER BY created_at DESC
LIMIT 20;
```

sell_now > 0인 사이즈가 있는지 확인. 없으면 "크림 시세 없음 — 수익 계산 불가" 진단.

### 3. 소싱처 검색 테스트 (GET 전용)

4개 소싱처에서 모델번호로 실제 검색:

- **무신사**: `https://goods-search.musinsa.com/api/v2/search?keyword={모델번호}&size=5`
- **29CM**: `https://search-api.29cm.co.kr/api/v4/products?keyword={모델번호}&limit=5`
- **나이키**: `https://www.nike.com/kr/t/{모델번호}` (PDP 직접)
- **아디다스**: `https://www.adidas.co.kr/api/plp/content-engine/search?query={모델번호}`

각 요청에 2초 간격, 타임아웃 10초.

결과 기록:
- HTTP 상태코드
- 검색 결과 수
- 매칭 가능한 상품명/가격

### 4. 매칭 시뮬레이션

`src/matcher.py`의 `normalize_model_number()` 로직 확인:
- 소싱처 결과의 모델번호 추출
- 정규화 후 크림 모델번호와 exact match 비교
- 매칭 실패 시 원인 분석 (SKU vs 품번, 하이픈 차이 등)

### 5. 수익 계산 검증

매칭 성공 시:
```
정산가 = sell_now - (2,500 + sell_now × 0.06) × 1.1 - 3,000
순수익 = 정산가 - 소싱처_가격
ROI = 순수익 / 소싱처_가격 × 100
```

시그널 판정:
- STRONG_BUY: 순수익 ≥ 30,000 AND volume_7d ≥ 10
- BUY: 순수익 ≥ 15,000 AND volume_7d ≥ 5
- WATCH: 순수익 ≥ 5,000 AND volume_7d ≥ 3

### 6. 스캔 스케줄 진단

- `scan_priority` 적정성: volume_7d 기준 hot/warm/cold 맞는지
- `next_scan_at` 적정성: TTL 기준 정상 범위인지
- `last_batch_scan_at` 확인: 최근 스캔 시각

### 7. 캐시/알림 이력

```sql
-- 알림 이력
SELECT * FROM alert_history
WHERE model_number LIKE '%{모델번호}%'
ORDER BY created_at DESC LIMIT 10;
```

`data/scan_cache.json`에서 해당 모델번호 캐시 엔트리 확인.

## 보고 형식

```markdown
## 스캔 디버그: {모델번호}

### 1. 상품 정보
| 항목 | 값 |
|------|---|

### 2. 크림 시세
| 사이즈 | sell_now | buy_now | 최근거래가 |
|--------|---------|---------|-----------|

### 3. 소싱처 검색
| 소싱처 | HTTP | 결과수 | 매칭 여부 | 가격 |
|--------|------|--------|----------|------|

### 4. 매칭 분석
- 매칭 성공/실패 원인
- 모델번호 정규화 비교

### 5. 수익 계산
| 소싱처 | 사이즈 | 매입가 | sell_now | 순수익 | ROI | 시그널 |
|--------|--------|--------|---------|--------|-----|--------|

### 6. 스케줄 상태
- Priority: ... (적정 여부)
- 다음 스캔: ...
- 마지막 스캔: ...

### 7. 진단 결론
- 파이프라인 정상 / 이상
- (이상 시) 병목 구간: [크림 시세 없음 / 소싱처 검색 실패 / 매칭 실패 / 수익 미달 / 스케줄 이상]
- 권장 조치: ...
```

## 안전 규칙

- **GET 전용**: POST, PUT, DELETE, PATCH 절대 금지
- **요청 간격 2초 이상** 유지 (소싱처별)
- **429 받으면 30초 대기** 후 재시도 (최대 2회)
- DB 읽기(SELECT)만 — INSERT/UPDATE/DELETE 금지
- 크림 API 직접 호출 금지 (DB 데이터만 사용)
- 디버깅 전용, 수정 작업은 다른 에이전트에 위임
```

- [ ] **Step 2: 커밋**

```bash
git add .claude/agents/scan-debugger.md
git commit -m "feat: scan-debugger 에이전트 추가 — 모델번호별 파이프라인 추적"
```

---

### Task 7: 최종 검증 + 통합 커밋

- [ ] **Step 1: 전체 파일 존재 확인**

```bash
ls -la .claude/commands/health.md .claude/commands/queue.md .claude/commands/trace.md
ls -la .claude/agents/queue-inspector.md .claude/agents/scan-debugger.md
```

- [ ] **Step 2: CLAUDE.md에 "(구현 예정)" / "(v2 예정)" 잔존 여부 확인**

```bash
grep -n "구현 예정\|v2 예정" CLAUDE.md
```

예상: 0건 매칭

- [ ] **Step 3: pytest 실행**

```bash
python3 -m pytest tests/ -v
```

기존 테스트 깨지지 않는지 확인. (Markdown 파일만 추가했으므로 영향 없어야 함)

- [ ] **Step 4: verify.py 실행**

```bash
PYTHONPATH=. python3 scripts/verify.py
```
