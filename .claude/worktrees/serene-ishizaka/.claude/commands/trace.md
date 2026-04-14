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
ORDER BY kph.fetched_at DESC
LIMIT 5;

-- 거래량 스냅샷 (최근 5건)
SELECT kvs.*
FROM kream_volume_snapshots kvs
JOIN kream_products kp ON kp.product_id = kvs.product_id
WHERE kp.model_number LIKE '%{모델번호}%'
ORDER BY kvs.snapshot_at DESC
LIMIT 5;

-- 알림 이력
SELECT ah.*
FROM alert_history ah
JOIN kream_products kp ON kp.product_id = ah.kream_product_id
WHERE kp.model_number LIKE '%{모델번호}%'
ORDER BY ah.created_at DESC
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
