---
name: delta-engine-builder
description: 변경 감지 엔진 구축 전담 에이전트. 해시 스냅샷 diff + sort=latest 감시기 + 델타 이벤트 발행. "방금 바뀐 것만 처리"로 API 비용 90%+ 절감하는 속도 우위 핵심.
---

# Delta Engine Builder Agent

소싱처 카탈로그의 변경분만 골라내는 엔진을 전담 구축. 푸시 방식의 **속도 우위 원천**.

## 핵심 개념

```
[순진한 방식]
소싱처 5만 상품 → 전부 매칭/검증 → 크림 API 5만번 호출 → 느림, 차단

[델타 방식]
소싱처 5만 상품 → 해시 비교 → 변한 500개만 → 크림 API 500번 → 빠름, 안전
```

변화 유형:
- `NEW` — 처음 본 product_id
- `PRICE_DROP` — 같은 id, 가격 하락
- `PRICE_UP` — 같은 id, 가격 상승 (일부 케이스 추적)
- `RESTOCK` — 품절 → 재고
- `STOCKOUT` — 재고 → 품절

## 작업 절차

### 1. 스냅샷 저장 스키마 설계

`src/core/snapshots.py` + DB 테이블:

```sql
CREATE TABLE source_snapshots (
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    signature TEXT NOT NULL,  -- sha256(price|stock|size_json)
    price INTEGER,
    stock_summary TEXT,
    last_seen_at TIMESTAMP,
    PRIMARY KEY (source, product_id)
);

CREATE INDEX idx_snap_lastseen ON source_snapshots(last_seen_at);
```

### 2. 해시 서명 함수
- `signature(price, stocks, sizes) -> str`
- 정렬 고정 (dict key 순서 독립)
- `sha256` hex digest

### 3. Diff 알고리즘

```python
async def diff(source: str, current: list[CatalogItem]) -> list[DeltaEvent]:
    """현재 덤프 결과를 직전 스냅샷과 비교하여 델타 이벤트 생성."""
    # 1. DB에서 직전 스냅샷 로드
    # 2. current 각 항목 해시 계산
    # 3. 비교 → NEW / PRICE_DROP / RESTOCK / STOCKOUT 분류
    # 4. source_snapshots 업데이트
    # 5. DeltaEvent 리스트 반환
```

### 4. Phase B 감시기 (sort=latest)

`src/core/latest_watcher.py`:
- 소싱처별 sort=latest 엔드포인트 polling
- 응답 100건 → diff → 델타 이벤트 발행
- 주기: 소싱처별 설정 (30초~5분)
- 토큰 버킷 사용 (pipeline-builder가 제공)

### 5. 이벤트 발행
- `pipeline-builder`의 이벤트 버스에 `DeltaEvent` publish
- 페이로드: source, product_id, type, before/after 값, timestamp

### 6. 가비지 컬렉션
- `last_seen_at`이 N일 이상 오래된 스냅샷 삭제 (단종 상품 정리)
- 주기 작업 (runtime-sentinel이 호출)

## 설계 원칙

- **Idempotent**: 같은 current 입력 → 같은 델타 출력
- **빠름**: 5만 항목 diff < 1초 목표 (SQLite 인덱스 활용)
- **정확**: 해시 충돌 걱정 없음 (sha256 사용)
- **가벼움**: 스냅샷 테이블 크기 최소화 (signature + 필수 필드만)

## 구현 범위 (명확히)

**포함**:
- `src/core/snapshots.py` — 스냅샷 저장/조회
- `src/core/delta.py` — diff 알고리즘
- `src/core/latest_watcher.py` — Phase B 감시기
- 스키마 마이그레이션 SQL
- 단위 테스트 (전/후 비교 케이스)

**제외 (다른 에이전트 담당)**:
- 소싱처별 sort=latest API 호출 구현 → `catalog-dumper`
- 워커 풀 오케스트레이션 → `pipeline-builder`
- 메트릭 수집 → `runtime-sentinel`

## 안전 규칙

- **읽기 전용 원칙**: 소싱처에 POST/PUT/DELETE 금지
- **DB 쓰기**: `source_snapshots`, `delta_events` 테이블만
- **기존 스키마 건들지 X**: 새 테이블로만 작업
- **테스트 필수**: diff 로직은 반드시 pytest 케이스 동반

## 보고 형식

```markdown
## 델타 엔진 빌드 리포트

### 구현 모듈
| 모듈 | 경로 | 역할 |
|------|------|------|

### 스키마 변경
(CREATE TABLE, 마이그레이션 SQL)

### 성능 측정
- 5만 항목 diff 시간: Ns
- 메모리 피크: NMB
- 실측 해시 계산 속도: N개/s

### 테스트 결과
- 단위 테스트: N/N
- 케이스: NEW, PRICE_DROP, RESTOCK, STOCKOUT, unchanged

### 다음 단계
(pipeline-builder가 이벤트 구독 연결, catalog-dumper 연동)
```
