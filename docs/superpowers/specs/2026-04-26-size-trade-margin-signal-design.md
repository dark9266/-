# 사이즈별 체결가 + Dual-Scenario 마진 시그널 설계

**작성일**: 2026-04-26 / 마지막 갱신: 2026-04-27
**상태**: 6건 라이브 검증 완료. 보수 추정 알고리즘 확정. Plan 작성 대기 중.

## Context

### 문제

현재 봇 마진 계산은 **단일 anchor**:
- 크림 즉시판매가 (`sell_now_price`) 기준 단일 마진
- 소싱처 정가 (할인 미반영) 기준 단일 시나리오

→ **놓치는 케이스**:
1. 즉시판매가 마진은 작지만 체결가 등록 시 마진 큰 상품
2. 정가 기준 마진 X 지만 사용자 보유 쿠폰/카드할인 적용 시 마진 큰 상품 (사용자 edge)
3. 가격 떨어지는 상품인데 즉시 마진만 보고 매수 알림 (추세 무시)

→ **사용자 경쟁력 손실**:
- 다른 봇/리셀러도 정가 기준 동일 판단 = edge 부재
- 사용자만 아는 쿠폰/카드/적립 = 활용 시 진짜 차별화

### 의도

**1. Dual-Anchor 마진** = 즉시판매가 + 체결가 둘 다 표시 → 사용자가 즉시매도 vs 등록판매 결정.
**2. 추천 별점** = 가격/거래량 추세 + 절대 거래량 종합 → 마진 외 신호 추가.
**3. Dual-Scenario 시나리오** = 정가 + 보정가(자동 할인 적용) 둘 다 평가 → 봇이 놓친 edge 회복.

### 수익 영향

스크린샷 IF2857-700 케이스:
- 정가 159,000원 → 모든 사이즈 손해 (-10k ~ -61k)
- 사용자 결제가 139,100원 (10% 쿠폰 + 4k 카카오) → 275 사이즈 +9,260원 ✅
- = **24,000원 차이 (15%)** = 봇이 자동 적용 시 알림 발사 가능

---

## 결정 사항 (사용자 확정)

| 항목 | 값 | 근거 |
|---|---|---|
| **수수료 공식** | `(2,500 + 판매가 × 6%) × 1.1 + 3,000` | 크림 화면 9,480원 = 코드 9,482원 일치 |
| **알림 하드 플로어** | 순수익 ≥ 10,000원 AND ROI ≥ 5% AND 거래량 ≥ 1 | 현 default 유지 |
| **WATCH** | 즉시판매가 마진 ≥ 5,000원 | 현 default 유지 |
| **BUY** | 즉시판매가 마진 ≥ 15,000원 | 현 default 유지 |
| **STRONG_BUY** | 즉시판매가 마진 ≥ 30,000원 | 현 default 유지 |
| **체결가 표시** | embed에 항상 표시, 게이트 X | 사용자 직접 판단 (B안) |
| **거래량 게이트** | ≥ 1 (숨은 보석 정책 유지) | INVARIANT |

## 가격 기준 정의

| 명칭 | DB 컬럼 | 의미 |
|---|---|---|
| 즉시판매가 | `kream_price_history.sell_now_price` | 셀러 즉시 매도 시 받는 돈 (구매자 입찰 최고가) |
| 체결가 | `kream_price_history.last_sale_price` | 마지막 실제 거래가 |
| 즉시구매가 | `kream_price_history.buy_now_price` | 셀러 등록판매 시 도달 가능 상한 (구매자 즉시 사가는 호가) |

**항상 `sell_now < last_sale < buy_now`** (정상 시장).

---

## 설계 — 3 Phase 구조

### Phase 1: Dual-Anchor Display (즉시 구현)

#### 의도
즉시매도/등록판매 의사결정 정보 알림 embed 에 표시. 시그널 게이트는 즉시판매가 기준 유지 (보수).

#### 변경

**`src/profit_calculator.py`**:
```python
@dataclass
class SizeProfitResult:
    size: str
    retail_price: int
    kream_sell_now_price: int      # 기존
    kream_last_sale_price: int     # 신규 (nullable, 데이터 없으면 0)
    kream_buy_now_price: int       # 신규 (nullable, 데이터 없으면 0)
    fees: dict
    net_profit_sell_now: int       # 즉시판매 시 (시그널 등급 기준)
    net_profit_last_sale: int      # 체결가 등록 시 (정보 표시용)
    roi_sell_now: float
    signal: Signal                 # net_profit_sell_now 기반
```

알림 발사 조건 (변경 없음):
- `net_profit_sell_now ≥ 10,000` AND `roi_sell_now ≥ 5%` AND `volume_7d ≥ 1`

#### Discord embed 표시
```
[Adidas Samba IH1325] 270mm
시그널: BUY ★★

소싱: 80,000원 (29CM)
즉시 던지면: +12,520원 (즉시판매가 102,000)
체결가 등록: +17,260원 (체결가 105,000)  ← 보너스 정보
─────────
거래량 7d 12건 / 30d 45건
[크림 보기] [29CM 보기]
```

#### 수익 영향 (시뮬레이션)
- 알림 수: 변화 없음 (게이트 동일)
- 사용자 의사결정 개선: 즉시 vs 등록 정보 1초 안에 판단 가능

### Phase 2: 추천 별점 (시계열 데이터 1주 누적 후 활성)

#### 의도
마진 외 추가 신호 (가격 추세 + 거래량 추세) 로 추천도 평가.

#### 데이터 의존성
- `kream_price_history` (시계열, 갱신 루프 부활 후 누적 시작)
- `kream_volume_snapshots` (1주 전 vs 현재 비교)
- 1주 누적 전엔 별점 표시 X (폴백)

#### 점수 계산 (3축 합산, 각 ±1점)

| 축 | +1점 | 0점 | -1점 |
|---|---|---|---|
| 가격 추세 (체결가 7일 변화) | +5% 이상 ↑ | -5%~+5% | -5% 이상 ↓ |
| 거래량 추세 (volume_7d 1주 변화) | 2배 이상 ↑ | 0.8~2배 | 0.8배 이하 ↓ |
| 절대 거래량 (volume_7d) | ≥ 5건 | 1~4건 | 0건 |

#### 별점 매핑
- **+3점 = ★★★ 강력추천** (가격 ↑ + 거래량 ↑ + 활발)
- **+2점 = ★★ 추천**
- **+1점 = ★ 보통** (마진 있지만 추세 약함)
- **0점 이하 = 추천 X** (시그널 BUY 여도 별점 표시 X 후 신중 라벨)

#### 변경

**신규 `src/recommendation.py`**:
```python
def calculate_recommendation(
    product_id: str,
    volume_7d: int,
    db: aiosqlite.Connection,
) -> dict:
    """추천 점수 계산. 시계열 데이터 부족 시 None 폴백."""
    price_trend = await _price_trend_7d(product_id, db)  # +1/0/-1 또는 None
    volume_trend = await _volume_trend_7d(product_id, db)  # +1/0/-1 또는 None
    abs_volume = +1 if volume_7d >= 5 else (0 if volume_7d >= 1 else -1)

    if price_trend is None or volume_trend is None:
        return {"score": None, "stars": "", "reasons": ["시계열 누적 중"]}

    score = price_trend + volume_trend + abs_volume
    stars = "★★★" if score >= 3 else "★★" if score >= 2 else "★" if score >= 1 else ""
    return {"score": score, "stars": stars, "reasons": [...]}
```

**`src/discord_bot/bot.py`** embed 에 추천 라인 추가:
```
시그널: BUY ★★ 추천
└ 가격 ↑ +8% (1주)
└ 거래량 ↑ 3배 (5건 → 12건)
└ 7d 거래 12건 (활발)
```

### Phase 3: Dual-Scenario 마진 (자동 할인 매칭)

#### 의도
정가 + 사용자 보정가 둘 다 평가 → 사용자만의 edge 자동 활용.

#### 자동 할인 매칭 (api-prober 결과 반영)

**자동 추출 (goods API 응답에서)**:
| 필드 | musinsa_httpx 처리 |
|---|---|
| `goodsPrice.salePrice` | 등급할인 적용가 (이미 추출) |
| `goodsPrice.couponPrice` | 회원 쿠폰 적용가 (이미 추출, 0 아니면 우선) |
| `goodsPrice.savePoint` | 적립 포인트 정액 (**신규 추가**) |
| `goodsPrice.savePointPercent` | 적립률 (**신규 추가**) |
| `goodsDetailBanner.benefitBanner` | 이벤트 배너 (**신규 추가**, 알림 표시용) |
| `isExclusiveMusinsaPay`, `isExclusiveMusinsaHyundaiCard` | 결제수단 제한 (**신규 추가**) |

**상수 테이블 (api 로 못 받는 정보, config 하드코딩)**:
```python
class SourceDiscountConstants(BaseSettings):
    """결제 페이지 GET 차단으로 자동 추출 불가 → 사용자 평균값"""
    card_instant_discount_won: int = 0  # 카드 즉시할인 (사용자 보유 카드별 평균)
    point_use_max_rate: float = 0.0     # 적립금 사용 한도 (예: 무신사 0.07)
    user_point_balance: int = 0          # 사용자 보유 적립금 (수동 입력 또는 마이페이지 GET)

source_discount_constants: dict[str, SourceDiscountConstants] = {
    "musinsa": SourceDiscountConstants(
        card_instant_discount_won=4000,    # 카카오페이 × 페이머니/BC카드 평균 (사용자 보유 시)
        point_use_max_rate=0.07,            # 무신사 적립금 7% 한도 (goods API maxUsePointRate)
        user_point_balance=14147,           # 사용자 입력 (마이페이지 GET 시 자동 갱신)
    ),
    "29cm": SourceDiscountConstants(card_instant_discount_won=0),  # 사용자 검토
    # ... 사용자 추후 확정
}
```

**제외 — 1회성 보너스 (반영 X)**:
- 무신사머니 첫 결제 보너스 (8,720원 같은) → 사용자 본인 1회성, 봇 자동 반영 X
- 신규 가입 쿠폰 → 1회성, 미반영
- 이벤트 한정 적립 부스트 (생일 쿠폰 등) → 사용자 직접 인지, 봇 미반영

**근거**: 1회성 보너스 자동 반영 시 마진 추정 과대 → 사용자 알림 신뢰도 손실. 반복 적용 가능한 할인만 자동 반영 원칙.

⚠️ **이 상수는 사용자 보유 카드 상황 반영 필요**. 사용자가 사용 안 하는 결제수단은 0 으로. 보수적 추정 우선.

#### 보정가 계산
```python
def adjusted_retail_price(retail_product: RetailProduct, source: str) -> dict:
    """자동 추출 + 상수 적용한 보정가. 반복 적용 가능 할인만 반영 (1회성 X)."""
    base = retail_product.price  # = couponPrice or salePrice (이미 자동 적용)
    save_point = retail_product.save_point or 0  # 구매 시 적립 (이번 거래 비용 차감)
    
    consts = source_discount_constants[source]
    card_discount = consts.card_instant_discount_won
    
    # 적립금 사용 한도 (보유 적립 × 한도율, 단 base 의 한도율 초과 X)
    point_use = min(
        int(consts.user_point_balance * consts.point_use_max_rate),
        int(base * consts.point_use_max_rate),
    )
    
    adjusted = base - card_discount - save_point - point_use
    return {
        "adjusted": adjusted,
        "breakdown": {
            "base": base,
            "card_discount": card_discount,
            "save_point": save_point,
            "point_use": point_use,
        }
    }
```

**`src/profit_calculator.py`** dual-scenario 평가:
```python
@dataclass
class SizeProfitResult:
    # ... 기존 필드
    # 시나리오 1 (정가): 기존 retail_price 그대로
    net_profit_nominal: int
    # 시나리오 2 (보정가): retail_price - discounts + accrual
    net_profit_adjusted: int
    adjusted_retail_price: int  # 보정 후 실효 소싱가
    discount_breakdown: dict     # {"coupon": 6360, "card": 4000, "accrual": 1391}

    signal: Signal  # 둘 중 더 높은 마진 기준
    signal_scenario: Literal["nominal", "adjusted"]  # 어느 시나리오로 발사
```

알림 발사 조건 (OR 게이트):
- `net_profit_nominal ≥ 10,000` AND `volume_7d ≥ 1`
- **OR**
- `net_profit_adjusted ≥ 10,000` AND `volume_7d ≥ 1`

#### Discord embed (예시)
```
[Nike ACG LDV M IF2857-700] 275mm
시그널: BUY (쿠폰 적용 시) ★★

🟡 정가 매수 시
   소싱: 159,000원
   체결가 등급: -10,640원 ❌

🟢 쿠폰 매수 시 ⭐
   소싱: 139,100원 (자동 -19,900)
   ├ 정기 쿠폰: -6,360원
   ├ 카카오 즉시할인: -4,000원
   ├ 봄맞이 10% (만료 39분): -15,900원  ← 시한 (api-prober 통과 시)
   └ 1% 적립: -1,391원
   체결가 등급: +9,260원 ✅

거래량 7d 1건 ⚠️
[크림 보기] [무신사 결제 페이지]
```

#### 시한 쿠폰 (TBD — api-prober 결과 의존)
- 봄맞이 10% 같은 시한 이벤트 쿠폰 = 결제 페이지 GET 으로 받을 수 있는지에 따라
- GET 가능: 자동 인지 + 만료 임박 시 알림에 표시
- POST 필요: 사용자 토글 (UI Phase A 이후)

---

## Edge Cases

| 케이스 | 처리 |
|---|---|
| 사이즈별 체결가 0 (거래 없음) | embed 에 "체결가 N/A" 표시, 즉시판매가만 평가 |
| 사이즈별 즉시판매가 0 (입찰 0건) | 알림 X (전형적 데드 사이즈) |
| 시계열 데이터 부족 (1주 미만) | 별점 표시 X, 마진만 표시 |
| Source 가 source_discounts dict 에 없음 | 정가 시나리오만 평가 |
| 적립률 적용 시 적립금이 정수 아님 | 반올림 처리 |

---

## 검증

### 단위 테스트 (`tests/test_profit_calculator.py`)

신규 테스트 케이스:
1. **체결가 0 인 사이즈** → `kream_last_sale_price = 0`, embed 표시 "N/A"
2. **dual-scenario 둘 다 마진** → signal_scenario = "nominal" (보수 우선)
3. **정가 마진 X, 보정가 마진 O** → signal_scenario = "adjusted", 알림 발사
4. **둘 다 마진 X** → 알림 X
5. **수수료 공식 회귀** → 102,000원 판매가 → 수수료 9,482원 (크림 화면 일치)

### 회귀 테스트 (`tests/fixtures/false_positives.json`)
- IF2857-700 케이스 추가 (정가 -10k, 보정가 +9k → 알림 가야 함)
- 거래량 0 상품 → 알림 X 검증

### 라이브 검증 (1주 누적)
- 알림 발사 건수 비교 (Phase 3 전후)
- "쿠폰 적용 시" 시그널 비율 확인
- 사용자 매수 결정 후 실 마진 vs 봇 추정 마진 차이 측정

### Canary
- 기존 20개 기준 페어 통과 확인 (`tests/fixtures/canary_matches.json`)

---

## 구현 순서

| 단계 | 의존성 | 예상 시간 |
|---|---|---|
| Phase 1 (Dual-Anchor) | 즉시 가능 | 2~3일 |
| Phase 2 (별점) | 시계열 1주 누적 후 활성 | 코드 1~2일 + 활성 1주 |
| Phase 3 (Dual-Scenario 정적) | api-prober 결과 무관 | 1~2일 |
| Phase 3 (시한 쿠폰 자동 인지) | api-prober 결과 의존 | TBD |

---

## api-prober 결과 (2026-04-26 23:35 완료)

### 자동화 가능 영역 (GET only)

| 정보 | 출처 | 상태 |
|---|---|---|
| **회원 쿠폰 적용가** | `goods API.goodsPrice.couponPrice` | ✅ 이미 자동 반영 중 (`musinsa_httpx.py:202-205`) |
| **무신사머니 적립** | `goods API.goodsPrice.savePoint` + `savePointPercent` | ✅ 신규 수집 추가 |
| **이벤트 배너 존재** | `goods API.goodsDetailBanner.benefitBanner` | ✅ 신규 수집 추가 (이름만, 코드 X) |
| **결제수단 제한** | `isExclusiveMusinsaPay`, `isExclusiveMusinsaHyundaiCard` | ✅ 필터링 용 |

### 자동화 불가 영역 (POST 다단계 필요 = 위험)

| 정보 | 차단 사유 |
|---|---|
| **카드 즉시할인** (-4,000원 등) | 결제 페이지 진입 필요 (`api.musinsa.com/api2/payment/*` 전부 404). POST cart → 결제 페이지 → Bearer 토큰 발급 = 3단계 위험 |
| **쿠폰 상세 목록** (만료/조건) | `api2/coupon/available` 401 — 세션 쿠키 X, Bearer 토큰 prerequisite 필요. 토큰 GET 발급 경로 미발견 |
| **이벤트 쿠폰 코드/조건** | 배너 이름만 있고 상세 endpoint 404 |

### 종합 판정

**POST 룰 안 깨고 ~80% 커버 가능**:
- 회원 쿠폰가 (자동) ✅
- 등급할인 (자동, sale_price) ✅
- 적립 (자동, savePoint) ✅
- 카드 즉시할인 → **상수 테이블 하드코딩** (보수적 평균값) ⚠️
- 이벤트 쿠폰 → 봇이 "이벤트 있음" 알림 → 사용자가 직접 발급/적용 ⚠️

**POST 룰 변경 검토 = 위험 대비 이득 부족** (~20% 추가 커버 위해 메인 계정 BAN 위험 부담).

### 결정

- **Phase 3 정적 부분 = 즉시 진행 가능** (자동 할인 매칭, 약 80% 커버)
- **Phase 3 동적 부분 = 영구 미구현** (POST 룰 깨면서까지 자동화 X)
- **시한 쿠폰 = "있음" 알림만** + 사용자 수동 발급/적용

---

## 2026-04-27 라이브 검증 결과 (6건)

### 6건 실 결제 vs 봇 추정 비교

| # | 상품 | 정가 | 봇 추정 (낙관) | 봇 추정 (보수) | 실 결제 | 갭 (보수) |
|---|---|---:|---:|---:|---:|---:|
| 1 | adidas EVO SL (6011260) | 219,000 | 214,010 | ~ 207,000 | 189,900 | +17k |
| 2 | adidas KD1517 (6121682) | 159,000 | 142,280 | 142,400 | **142,400** | **0 ✅** |
| 3 | Nike Jordan 1 Low (5870697) | 179,000 | 159,806 | ~ 152,000 | 140,990 | +11k |
| 4 | Nike AWF 재킷 (6131130) | 135,000 | 131,000 | ~ 124,000 | 122,900 | **1.1k ✅** |
| 5 | Nike 샥스 Z W 타투 (5797225) | 159,000 | 142,392 | ~ 136,000 | 126,300 | +10k |
| 6 | Nike 샥스 Z W 블랙 (5797223) | 159,000 | 127,753 | ~ 136,000 | 135,000 | **1.5k ✅** |

### ⭐ 결정적 발견

**같은 모델 다른 색상에 쿠폰 적용 다름**:
- 5797225 (타투): 나이키 2주년 -15,900 적용됨 → 126,300원
- 5797223 (블랙): 정기 -6,360만 적용됨 → 135,000원

→ **마이페이지 "max 보유 쿠폰 자동 적용" 가정 = 거짓 알림 위험**.

낙관 추정 채택 시 5797223 케이스: 봇 127,753 / 실 135,000 = **봇이 마진 7,247원 큰 척 → 거짓 알림**.

### ⭐ 보수 추정 알고리즘 채택

```python
def adjusted_price(api, user, mypage_data):
    base = api['couponPrice']
    
    # ① 정기 쿠폰만 자동 적용 (모든 상품 적용 가정 — 보수)
    regular_coupons = [c for c in mypage_data['general_coupons']
                       if c.is_regular and not c.expired()]
    if regular_coupons:
        base -= max(regular_coupons, key=lambda c: c.amount).amount
    
    # ② 시한 쿠폰 = 정보만 표시 (시그널 영향 X)
    # → embed 에 "추가 가능: -X원 (시한 쿠폰)" 표시
    
    # ③ 적립금 사용 (isRestictedUsePoint 체크)
    if not api['isRestictedUsePoint']:
        base -= min(user.balance, int(base * api['maxUsePointRate']))
    
    # ④ 선할인 (isPrePoint 별도 체크)
    if api['isPrePoint']:
        base -= int(base * user.grade_save_rate)
    
    # ⑤ 카드 max (7개 룰 자동 매칭)
    base -= best_card_discount(base, user.cards)
    
    return base
```

→ **봇 추정 ≥ 실 결제** 항상 보장 = ① 정확성 축 (거짓 알림 0) 부합.
→ 사용자 결제 시 시한 쿠폰 catch 가능 시 보너스 마진 (예상보다 더 남음).

### 알림 embed 표시 (보수 추정 + 시한 정보)

```
[Nike 샥스 Z W (HQ7540-500)] 270mm
시그널: BUY (정기 쿠폰 -6k 기준)
추천: ★★

🟢 보수 마진 추정 (안정 보장)
   소싱: ~136,000원 (정기쿠폰 + 적립 + 카드)
   체결가 등록: +X원 ✅

💡 추가 마진 가능 (시한/카테고리 조건부)
   - 나이키 2주년 -15,900원 (만료 23h)
     → 적용 시 추가 +Y원
   
거래량 7d 5건
[크림 보기] [무신사 보기]
```

### 사용자 정보 입력 받음 (이번 세션)

- LV.4 브론즈, 1% 적립
- 보유 적립금: 14,147원
- 카드 보유: **모든 카드사 거의 다** (7개 룰 전부 활성)
- 카드 즉시할인 7개 룰 표:
  1. 무신사페이 × BC카드: -4,000원 (10만원↑)
  2. 무신사페이 × 농협카드: -3,000원 (8만원↑)
  3. 토스페이 × 삼성카드: -5,000원 (15만원↑)
  4. 토스페이 × 롯데카드: -3,000원 (8만원↑)
  5. 토스페이 × 계좌: -3,000원 (8만원↑)
  6. 카카오페이 × 우리카드: -3,000원 (7만원↑)
  7. 카카오페이 × 페이머니: -4,000원 (10만원↑)

### isRestictedUsePoint / isPrePoint 플래그 검증

- `isRestictedUsePoint=True` → 적립금 사용 X (5870697, 6131130 케이스 검증)
- `isPrePoint=True` → 선할인 가능 (별도 메커니즘, isRestictedUsePoint 와 분리)
- `isLimitedCoupon` 플래그는 신뢰 X (False 인데 시한 쿠폰 적용된 케이스 다수)

### 무신사 ToS 검토 결과

- 매크로 사용 = **즉시 탈퇴** 명시 (무신사 공지)
- 쿠폰 부정 획득 = 1년 정지
- 결제 페이지 시뮬레이션 = 영구 X (메인 계정 BAN 위험)
- 마이페이지 GET (1시간 1회) = **안전권** (정상 사용자 행동)
- 자동 발급/결제 = 영구 X

### 진행 상태

- ✅ Spec 작성 + commit (`69a3252`)
- ✅ Spec 보강 (api-prober 결과 + 6건 검증) — 본 갱신
- ✅ 보수 추정 알고리즘 확정
- ⏳ Spec 최종 commit (다음 세션)
- ⏳ writing-plans skill 호출 → 단계별 plan
- ⏳ Phase 1 (Dual-Anchor) 구현 진입


## 핵심 파일 경로

- `src/profit_calculator.py` (신규 dual-anchor + dual-scenario 로직)
- `src/recommendation.py` (신규 — 별점 계산)
- `src/config.py` (신규 SourceDiscount dict)
- `src/discord_bot/bot.py` (embed 표시 변경)
- `src/orchestrator.py` (알림 발사 OR 게이트)
- `tests/test_profit_calculator.py` (회귀)
- `tests/fixtures/false_positives.json` (IF2857-700 케이스 추가)

---

## Phase 게이트 관계 (명확화)

알림 발사 흐름 (Phase 1 → 3 누적):

```
Phase 1 단독 (체결가 표시만):
  알림 발사 = 즉시판매가 마진 ≥ 10,000원 + 거래량 ≥ 1

Phase 3 추가 (dual-scenario 활성화 후):
  알림 발사 = (정가 시나리오 마진 ≥ 10,000원) OR (보정가 시나리오 마진 ≥ 10,000원)
            AND 거래량 ≥ 1
  
  시그널 등급 = 둘 중 더 큰 마진 기준 (10k=WATCH, 15k=BUY, 30k=STRONG)
  embed 라벨 = "정가 BUY" / "쿠폰 BUY (보정가 적용 시)"
```

Phase 2 (별점) 은 시그널 등급과 독립. 알림 발사 후 부가 정보로 표시.

## 시계열 데이터 부족 정의 (Phase 2)

별점 폴백 조건:
- `kream_price_history` 7일 전 같은 product_id+size 데이터 없음
- 또는 `kream_volume_snapshots` 1주 전 같은 product_id 데이터 없음

폴백 시 별점 표시 X, 마진 정보만 표시. 갱신 루프 부활(2026-04-26)부터 1주 누적 후 자연 활성.

## 명시 결정 (settled)

다음 결정은 본 spec 의 설계 기반. 변경 시 spec 재작성 필요:

1. **즉시판매가 = 시그널 게이트 기준** (체결가는 표시만)
2. **알림 임계값 default 유지** (10k 플로어, 15k BUY, 30k STRONG)
3. **수수료 공식 VAT 1.1 유지** (크림 화면 일치)
4. **거래량 게이트 ≥ 1** (숨은 보석 정책)
5. **추천 별점 3축 합산** (가격 추세 + 거래량 추세 + 절대 거래량)
6. **Phase 3 dual-scenario = OR 게이트** (정가 또는 보정가 마진 → 알림)
7. **시한 쿠폰 자동 발급 = 안 함** (POST 룰 + ToS 위반 위험)
8. **카드 즉시할인 = 상수 테이블** (api-prober 결과 GET 불가 확정, 결제페이지 진입 위험으로 자동화 영구 X)
