# 크림 보관판매 API 탐색 결과 (2026-05-01)

탐색 범위: 코드베이스 + JS 번들 정적 분석 + 공개 정보 조사  
분석 방법: curl_cffi (인증 없음) + Nuxt JS 번들 역분석

---

## 요약

보관판매 등록은 **2단계 API 시퀀스**로 이루어진다.

- 1단계(슬롯 시도): `POST /api/seller/inventory/stock_request/review` — 슬롯 가용성 확인 + 수량 제출
- 2단계(컨펌): `POST /api/seller/inventory/stock_request/confirm` — 실제 등록 확정

둘 다 `api.kream.co.kr` 도메인에 `X-KREAM-*` 인증 헤더 필요.  
사용자 세션 쿠키(webDid) + API 인증 헤더 조합.

---

## 1페이지 Endpoint — 슬롯 진입 시도

### 방법 A: stock_request/review (추천 경로)

```
POST https://api.kream.co.kr/api/seller/inventory/stock_request/review
Content-Type: application/json
```

**Request Payload:**
```json
{
  "items": [
    {
      "product_option": {
        "key": "<size_key>"
      },
      "quantity": <int>
    }
  ]
}
```

- `items`: 사이즈별 수량 배열. InventoryStockSizeRow 컴포넌트에서 사용자가 입력한 값.
- 신청 계속 버튼("신청 계속") 클릭 시 호출됨.

**Response (성공 — 슬롯 있음):**
```json
{
  "id": "<review_id>",
  "items": [...],
  "return_address": {...},
  ...
}
```
- `review_id` 를 2단계에서 사용.

**Response (슬롯 없음 — "창고 가득" 등):**
```json
{
  "code": 9999,
  "message": "<에러 메시지>"
}
```
- JS 코드에서 `e.code === 9999` 일 때 toast 에러 표시 후 재시도 루프로 복귀.
- HTTP 상태코드는 4xx (정확한 코드는 실 시도 캡처 필요).

### 방법 B: PATCH (기존 신청 수정)

```
PATCH https://api.kream.co.kr/api/seller/inventory/stock_request/review
Content-Type: application/json
```

**Payload:**
```json
{
  "id": "<review_id>",
  "items": [...],
  "delivery_method": "<string>",
  "return_address_id": <int>
}
```

기존 review_id가 있을 때 사용. 최초 신청에는 POST.

---

## 2페이지 Endpoint — 등록 컨펌

```
POST https://api.kream.co.kr/api/seller/inventory/stock_request/confirm
Content-Type: application/json
```

**Request Payload:**
```json
{
  "review_id": "<1단계 review_id>",
  "return_address_id": <int>,
  "return_shipping_memo": "<string>",
  "delivery_method": "<string>"
}
```

- `review_id`: 1단계 review POST 응답의 `id` 필드.
- `return_address_id`: 반송 주소 ID. 1단계 응답의 `return_address.id` 또는 별도 설정값.
- `return_shipping_memo`: 운송장 메모 (선택).
- `delivery_method`: 배송 방법 (선택).

**Response (성공):**
```json
{...}
```
- `paymentResponse` 에 저장됨.

---

## 연관 Endpoint 목록 (전체)

| 용도 | Method | URL | 비고 |
|------|--------|-----|------|
| **1페이지: 슬롯 신청** | POST | `/api/seller/inventory/stock_request/review` | 핵심. 슬롯 가용성 + 수량 |
| 1페이지: 기존 신청 수정 | PATCH | `/api/seller/inventory/stock_request/review` | 재신청 시 |
| **2페이지: 등록 확정** | POST | `/api/seller/inventory/stock_request/confirm` | 핵심. review_id 필요 |
| 내 보관 목록 조회 | GET | `/api/seller/inventory/items/{tab}` | tab = REQUESTED/IN_STOCK 등 |
| 내 보관 수량 현황 | GET | `/api/seller/inventory/counts` | shipment_required_count 등 |
| 보관 상세 조회 | GET | `/api/seller/inventory/items/{id}/` | 특정 항목 상세 |
| 반송 메모 | PUT | `/api/seller/inventory/actions/return-shipping-memos` | `{items: [{...ask_id}]}` |
| 반출 신청 | POST | `/api/seller/inventory/actions/review_retrieve` | `{items: [{ask_id: id}]}` |
| 보관판매 95점 목록 | GET | `/api/p/inventory_95/` | 상품별 95점 목록 |
| 판매 입찰 등록 (보관→판매) | POST | `/api/m/ask-requests` | 보관 후 판매 입찰 |
| 판매 입찰 화면 | GET | `/api/screens/ask-requests/{id}` | 입찰 확인 화면 |
| 판매 입찰 완료 | POST | `/api/screens/ask-request-completes` | 입찰 최종 컨펌 |

---

## 인증 흐름

### 세션 쿠키 (필수)
크림 웹 방문 시 자동 발급:
- `webDid`: 디바이스 ID. `X-KREAM-DEVICE-ID: web;{webDid}` 로 사용.

### API 인증 헤더 (필수)

기존 코드베이스(`kream.py:300-314`)에서 검증된 헤더 세트:

```python
{
    "X-KREAM-DEVICE-ID": f"web;{webDid_cookie}",
    "X-KREAM-CLIENT-DATETIME": "20260501120000+0900",  # 현재 시각
    "X-KREAM-API-VERSION": "56",  # HTML에서 apiVersion 추출
    "X-KREAM-WEB-REQUEST-SECRET": "kream-djscjsghdkd",  # 고정값
    "X-KREAM-WEB-BUILD-VERSION": "{buildVersion}",  # HTML에서 추출
}
```

### 로그인 (필수)
- 보관판매 endpoint는 인증 필요. 현재 `kream.py`는 pinia 전용 모드(로그인 없음).
- **보관판매 봇에서는 실 계정 로그인 필요**: `POST /api/m/login` 또는 세션 쿠키 직접 주입.
- 로그인 방식: 이메일/비밀번호 또는 네이버 OAuth (실 시도 시 캡처 필요).
- **401 Unauthorized 예상**: 비로그인 상태에서 seller/inventory 호출 시.

### 추가 토큰 필요 여부
코드 분석 결과 **추가 Bearer 토큰 불필요**. 세션 쿠키 + X-KREAM-* 헤더 조합으로 충분.

---

## 슬롯 가용성 감지 로직

JS 소스 기준 (`pf6Mts4b.js`):

```javascript
// G() = fetchReviewInventory()
async function G() {
  try {
    const t = await $http("/api/seller/inventory/stock_request/review", {
      method: "post",
      body: { items: validItems }  // quantity > 0 인 항목만
    });
    review.value = t;  // 성공 → review 저장
    return t;
  } catch (t) {
    if (t.data?.code === 9999) {
      // 슬롯 없음 or 오류 → toast 표시
      showToast({ content: t.data.message, type: "error" });
    }
    throw t;  // re-throw → 상위 catch → 재시도
  }
}
```

봇 구현 시 로직:
1. `code 9999` 응답 → 재시도 (슬롯 없음)
2. 정상 JSON 응답 + `id` 존재 → 슬롯 확보 → 2단계 진행
3. `401` → 세션 만료 → 재로그인

---

## Rate Limit

직접 관측 불가능. 추론:

| 근거 | 추정 |
|------|------|
| 크롬 확장 설명 "중복 클릭 방지" | 짧은 간격 반복 차단 가능성 |
| 크몽 매크로 "허용횟수 초과감지" | 요청 빈도 감지 존재 |
| 보판봇 시장 다수 가동 중 | 극단적 빈도 아니면 차단 없음 추정 |
| 다른 API 기준 | GET은 1~2초 간격. POST는 더 보수적으로 3~5초 권장 |

권장: 1페이지 시도 간격 **2~3초**. 429 수신 시 30초 대기.

---

## 구현 방법

크림 웹에서 로그인된 세션 쿠키를 그대로 재사용하는 방식이 가장 현실적:

1. **세션 주입 방식** (권장): 사용자가 브라우저에서 로그인 → 쿠키 추출 → 봇에 주입
2. **직접 로그인 방식**: `POST /api/m/sessions` 또는 `POST /api/m/login` 엔드포인트로 로그인 (미검증, 실 캡처 필요)

기존 `kream.py`의 `curl_cffi` Safari 핑거프린트 + `_init_cookies()` → `_build_api_auth_headers()` 패턴을 그대로 재사용 가능. 단, 로그인 세션만 추가.

---

## 다음 단계 — 실 시도 시 캡처 필요 항목

아래 항목은 **코드 분석만으로 확정 불가**, 사용자가 직접 보관판매 시도 시 Chrome DevTools → Network 탭 캡처 필요:

1. **로그인 endpoint + payload**: `/api/m/sessions` 또는 `/api/m/login` 정확한 경로 + request body
2. **슬롯 없음 응답 정확한 구조**: `code: 9999` + `message` 전체 텍스트 (예: "현재 보관 신청이 불가합니다")
3. **1단계 payload의 `items` 정확한 구조**: `product_option.key` 가 정확히 어떤 형식인지 (예: "265", "270" 등)
4. **2단계 confirm의 delivery_method 허용값**: "seller" / "kream" 등
5. **정확한 HTTP 상태코드**: 슬롯 없음 시 4xx (409? 503?) vs 200 with code 9999

---

## 안전 고려사항

- 다수 보판봇 가동 중 확인됨 (크롬 확장, 크몽 매크로, Threads 매크로) → BAN 위험 낮음
- 단, 동일 계정에서 초당 수십 회 요청 → 429 또는 IP 차단 위험
- 추천 패턴: 2~3초 간격, 429 시 30초 대기, 최대 3회 재시도
- 벌크 계정 분리 (`project_phase_b_storage_sale.md` 8 안전장치) 준수

---

## 탐색 방법 기록

- A. 코드베이스 grep: kream.py, kream_delta_client.py 분석
- B. 크림 웹 JS 번들: sell/0, inventory/:id 페이지 Nuxt 청크 역분석
  - `BbO5-lC6.js` (sell/:id 페이지) → `/api/m/ask-requests` POST 발견
  - `pf6Mts4b.js` (inventory 스토어) → **핵심 endpoint 전부 발견**
  - `Cp02JZ7M.js` (/my/inventory 페이지) → 관리 endpoint 발견
- C. 공개 정보: 크몽, Threads, Chrome Web Store — 기술 상세 없음, 동작 방식 정황 확인
- D. 모바일 앱 API: 미탐색 (별도 app.kream.co.kr 또는 다른 도메인 추정)
