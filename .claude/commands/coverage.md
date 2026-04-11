크림 DB 대비 소싱처 커버율 대시보드.

아래 정보를 수집하여 커버율을 분석해주세요:

## 수집할 정보 (SQLite 쿼리)

### 1. 크림 DB 전체 현황
```python
import sqlite3
db = sqlite3.connect('data/kream_bot.db')
c = db.cursor()

# 전체 상품 수
c.execute('SELECT COUNT(*) FROM kream_products')

# priority별 분포
c.execute('SELECT scan_priority, COUNT(*) FROM kream_products GROUP BY scan_priority')

# 브랜드별 분포 (상위 20)
c.execute('SELECT brand, COUNT(*) as cnt FROM kream_products GROUP BY brand ORDER BY cnt DESC LIMIT 20')
```

### 2. 소싱처별 커버 가능 브랜드
`src/crawlers/registry.py`와 각 크롤러 코드 분석:
- 각 소싱처가 커버하는 브랜드 목록
- 크림 DB 해당 브랜드 상품 수

### 3. 커버율 계산
```
소싱처 커버율 = Σ(소싱처가 커버하는 브랜드의 크림 상품 수) / 크림 DB 전체
```

## 출력 형식

```markdown
## 크림 DB 커버율 분석

### 브랜드별 분포 (크림 DB)
| 브랜드 | 상품수 | 비중 | 커버 소싱처 |
|--------|--------|------|-----------|

### 소싱처별 커버율
| 소싱처 | 커버 브랜드 | 커버 상품수 | 커버율 |
|--------|-----------|-----------|--------|

### 미커버 영역
| 브랜드 | 상품수 | 소싱처 없음 이유 |
|--------|--------|----------------|

### 커버율 개선 제안
(다음 추가할 소싱처 우선순위 + 근거)
```
