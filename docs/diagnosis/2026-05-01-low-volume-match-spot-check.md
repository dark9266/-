# 거래량 0 매칭 비중 비정상 4 어댑터 spot-check 진단

**일시**: 2026-05-01  
**대상**: thenorthface / nbkorea / puma / reebok  
**방법**: retail_products × kream_products DB 교차 분석 (DB 읽기 전용, 봇 무중단)

---

## 1. Spot-Check 분류 결과 (50건 샘플)

| 어댑터 | A (크림 미등재) | B (매칭O + vol=0) | C (매칭O + vol>0) | D (매칭 오류) |
|--------|:-:|:-:|:-:|:-:|
| thenorthface | 0% | 100% | 0% | 0% |
| nbkorea | 0% | 100% | 0% | 0% |
| puma | 0% | 100% | 0% | 0% |
| reebok | 0% | 100% | 0% | 0% |

- **모든 4 어댑터: 매칭 정확도 100%, vol=0이 유일한 차단 원인**
- 오매칭(D) 0건 — 모델번호가 retail == kream 완전 일치

### reebok 추가 분석

retail_products.model_number와 kream_products.model_number 단순 JOIN 시 35% 매칭으로 보이지만,
어댑터 `_load_kream_index()`의 슬래시 분할 확장 인덱스 적용 시 **100% 매칭** (225/225).

- 리복 공홈: `GY4967` 저장
- 크림 DB: `GY4967/100046497` 형식 저장
- 어댑터 런타임 확장: `GY4967` → 슬래시 파트 `GY4967` 인덱스 키로 자동 매핑 (정상 동작)

---

## 2. 가설 검증 결론

| 가설 | 설명 | 판정 |
|------|------|:----:|
| (a) 매칭 정확도 문제 | 잘못된 모델번호로 오매칭 | **X 불일치** |
| (b) 매칭 대상 선정 문제 | 인기 없는 상품만 덤프 | **X 불일치** |
| (c) 시계열 stale | 거래량 데이터 오래되어 현재 0 평가 | **O 일치 (변형)** |

**실제 근본 원인: (c') Cold-Tier Bootstrap 부재**

단순 "stale"이 아닌, 처음부터 한 번도 volume이 조회된 적 없는 상태:

```
신규 kream_products 수집
  → volume_7d = 0 (DEFAULT, 초기화값)
  → refresh_tier = 'cold'
  → last_volume_check = NULL
  
price_refresher는 HOT 티어만 실행
  → cold 티어는 영구 미조회

volume_spike_detector는 "이전 값 대비 2배 상승" 시 hot 승격
  → 이전 값이 없으면(NULL/0) spike 감지 불가
  
prefilter_low_volume 게이트: volume_7d < 1 → BLOCK
  → cold 티어 신규 상품 100% 영구 차단
```

---

## 3. 전체 규모 분석

### 크림 DB 거래량 현황

| 항목 | 수치 |
|------|------|
| 전체 kream_products | 48,466건 |
| volume_7d > 0 | 976건 (2.0%) |
| volume 한 번도 조회 안 됨 (last_volume_check=NULL) | 46,622건 (96.2%) |
| HOT 티어 (price_refresher 대상) | 323건 |
| Cold 티어 | 47,867건 |

### 브랜드별 거래량 데이터 보유 현황

거래량 데이터가 있는 브랜드: **Nike, Palace, Supreme, Jordan, Travis Scott, Levi's만**  
해당 브랜드들은 tier2_monitor (크림 hot 폴링)를 거쳐 hot 승격된 상품들.

4 진단 브랜드 (노스페이스, 뉴발란스, 푸마, 리복):  
- last_volume_check = NULL (전체, 100%)  
- volume_7d = 0 (전체, 100%)  
- kream_volume_snapshots = 0건 (전혀 없음)  

### 4 어댑터 매칭 현황

| 어댑터 | retail 고유 모델수 | kream 매칭 (확장 인덱스) | vol=0 차단 | vol>0 통과 |
|--------|:-:|:-:|:-:|:-:|
| thenorthface | 651 | 651 (100%) | 651 (100%) | 0 |
| nbkorea | 56 | 58 (104%)* | 58 (100%) | 0 |
| puma | 101 | 101 (100%) | 101 (100%) | 0 |
| reebok | 225 | 225 (100%) | 79 (직접 매칭)** | 0 |

*nbkorea 104%: 동일 모델번호의 폭 옵션(2A/D 등) 변형 상품이 크림에 복수 등재  
**reebok: 직접 JOIN 79건, 슬래시 확장 포함 225건 모두 매칭

### 14일 decision_log prefilter_low_volume 블록 규모

| 어댑터 | 14일 총 블록 | 일평균 |
|--------|:-:|:-:|
| thenorthface | 24,476 | 1,748 |
| puma | 6,160 | 440 |
| reebok | 5,100 | 364 |
| nbkorea | 4,210 | 300 |

---

## 4. 라이브 검증 (샘플 4건 kream product_id 확인)

| 어댑터 | 모델번호 | kream_product_id | kream 상품명 | vol_7d | refresh_tier |
|--------|----------|:-:|------|:-:|:-:|
| thenorthface | NM2DS07A | 772538 | 노스페이스 보레알리스 미니 백팩 블랙 - 26SS | 0 | cold |
| nbkorea | U990TB6 | 280863 | 뉴발란스 990v6 메이드 인 USA 트루 카모 | 0 | cold |
| puma | 312123-18 | 823111 | 푸마 디비에이트 나이트로 4 에스프레소 브라운 러셋 브라운 | 0 | cold |
| reebok | 100230347 | 641862 | (W) 리복 클래식 AZ 레드 | 0 | cold |

**매칭 정확도 4/4 PASS. vol=0 이 유일한 차단 원인 4/4 확인.**

---

## 5. Fix 옵션 비교

### Option 1: Cold-tier One-time Volume Bootstrap (권장)

- 4 브랜드 kream 매칭 상품 ~889건에 대해 kream `/api/p/options/display` 1회 호출
- volume_7d 실제값 반영 후 prefilter_low_volume 정상 작동
- 기대 효과: vol >= 1인 상품만 통과 → 거짓 알림 위험 0
- 비용: 889 kream API calls (10k 일일 캡의 8.9%)
- 범위 확장 가능: 22 어댑터 전체 매칭 상품으로 확장 시 수천건 (캡 검토 필요)

### Option 2: Fail-Open for Never-Checked Products (위험)

- `last_volume_check IS NULL`인 경우 prefilter 통과
- 현재 46,622건의 never-checked 상품이 존재
- 일일 매칭 수천건이 throttle로 쏟아져 12k+ throttle_exhausted 재현 위험
- 2026-04-adidas 사고 root cause와 동일한 패턴 → **채택 불가**

---

## 6. 다음 액션 제안

### 우선순위 1: 4 어댑터 Brand Bootstrap (단기, ~1시간)

대상 상품 수:
- thenorthface: 651건 (brand='The North Face' kream 매칭)
- nbkorea: 58건
- puma: 101건
- reebok: reebok expanded index 대상 ~225건

구현:
- `scripts/bootstrap_brand_volume.py` 작성
- 대상 product_id 목록 → `/api/p/options/display?product_id={id}` 배치 호출
- volume_7d, volume_30d, last_volume_check 업데이트
- 봇 가동 중 실행 가능 (DB 쓰기만, 봇 로직 무간섭)

### 우선순위 2: reebok kream_products 슬래시 확장 매칭 개선 (중기)

retail_products에 225건 모두 저장되어 있으나, kream과의 단순 JOIN으로는 79건만 매칭.
어댑터 런타임 인덱스는 정상이나, 모니터링/분석 쿼리에서 과소 집계 위험.
kream_products에 `model_number_parts` 컬럼 추가 또는 slash-expanded view 생성 검토.

### 우선순위 3: 전체 어댑터 Never-Checked Coverage 점검 (장기)

thenorthface/nbkorea/puma/reebok 외에도 다른 어댑터의 매칭 상품이
cold-tier never-checked 상태인지 확인.
stussy (1,867/일), arcteryx (1,853/일) 등도 동일 패턴 의심.

---

## 7. 결론 요약

| 질문 | 답 |
|------|---|
| 매칭 오류인가? | 아니오. 4 어댑터 모두 매칭 정확도 100% |
| 인기 없는 상품만 덤프하나? | 아니오. 전 카테고리 덤프 중 |
| 데이터 stale인가? | 부분적. 더 정확히는 "한 번도 초기화 안 된" 상태 |
| 실제 원인은? | Cold-tier 상품의 volume_7d 초기값=0이 prefilter 게이트를 영구 차단 |
| Fix는? | One-time volume bootstrap (4 브랜드 889건, ~캡 8.9%) |
