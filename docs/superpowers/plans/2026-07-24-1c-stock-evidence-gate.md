# 1c 정확성 게이트 — 소싱처 재고 증거 구현 계획

> **For agentic workers:** 각 조각은 `kream-executor`에 위임해 TDD로 구현. 조각 끝마다 머리가 검증.
> 근거: 코덱스 S/xhigh 자문(`reports/codex/codex_783b0f7aa2735fc46152.md`) + explorer 실측.

**Goal:** 소싱처 "사이즈 못 찾고 품절을 있다고 알림" 오탐을, `available_sizes=()`의 4중 의미를
상태 모델로 분리해 근본 해결한다. 알림은 **신선한 양성 재고가 증명된 것만**.

**Architecture:** `CandidateMatched → 소싱처 재고 검증(크림 前) → 크림 live GET → 사이즈 교집합
→ 하드게이트 → 알림`. UNKNOWN/만료 후보는 크림 호출·알림 금지, `verification_pending` 보류.

## Global Constraints (불변 — 매 조각 적용)
- 정확성 1순위: 품절/사이즈 오탐 0 지향. UNKNOWN을 "재고 있음"으로 해석 금지.
- 타겟 축소 금지: UNKNOWN 후보는 소싱처를 죽이는 게 아니라 보류(수집·매칭·재검증 유지).
- 크림 GET 전용·매칭 후보만·보수 딜레이. 소싱처 검증은 크림 호출 **전**.
- 하드플로어: 순익 ≥ 10,000 AND ROI ≥ 5% AND 7일 거래량 ≥ 1 (실시간 sell_now).

---

## 조각 1c-1: `SourceStockSnapshot` 상태 모델 + `CandidateMatched.source_stock`

**Files:** Create `src/models/stock.py` · Modify `src/core/event_bus.py`(CandidateMatched) · Test `tests/test_stock_snapshot.py`

**계약:**
```python
class StockState(str, Enum): IN_STOCK; OUT_OF_STOCK; UNKNOWN
@dataclass(frozen=True)
class SourceStockSnapshot:
    state: StockState
    available_sizes: tuple[str, ...] = ()   # IN_STOCK이면 비면 안 됨
    observed_at: float = 0.0                 # epoch (체크포인트 재생 시 오래된 재고 방지)
    expires_at: float = 0.0                  # 경과 시 자동 UNKNOWN 취급
    evidence_method: str = ""                # 어떤 경로로 관측했나
    reason_code: str = ""                    # UNKNOWN 사유(api_400/parse_fail/unsupported 등)
    def is_fresh(self, now: float) -> bool   # expires_at > now
    def usable(self, now: float) -> bool     # state==IN_STOCK and available_sizes and is_fresh
```
- `CandidateMatched`에 `source_stock: SourceStockSnapshot | None = None` 추가. **None = 명시적 UNKNOWN.**
- 기존 `available_sizes` 필드는 **하위호환 유지**(이후 조각에서 게이트가 source_stock 우선 참조).

**검증(TDD):** `SourceStockSnapshot` 생성·계약 테스트 — IN_STOCK인데 available_sizes 비면 usable=False;
expires_at 경과면 is_fresh=False→usable=False; None 취급. `pytest tests/test_stock_snapshot.py -v`.
기존 event_bus 테스트 전부 통과(하위호환). ruff.

---

## 조각 1c-2: 중앙 재고 게이트 (runtime) — UNKNOWN/만료 차단 + 보류

**Files:** Modify `src/core/runtime.py`(309-346 교집합 가드) · Test `tests/test_runtime_stock_gate.py`

- 현재 가드는 `available_sizes` 비면 **건너뜀**(미검증 통과). 이를 `source_stock` 기반으로:
  - `source_stock is None` 또는 `state==UNKNOWN` 또는 만료 → **크림 호출·알림 금지**, `verification_pending`
    로 로깅/보류(다음 사이클 재검증). drop이 아니라 보류로 구분.
  - `state==IN_STOCK and usable` → 기존대로 크림 사이즈 교집합. 비면 drop.
  - `state==OUT_OF_STOCK` → drop(품절 확정).
- `available_sizes`만 있고 source_stock 없는 레거시 candidate는 과도기: available_sizes를
  `SourceStockSnapshot(IN_STOCK, available_sizes, observed_at=now)`로 승격(단 observed_at 없으면 UNKNOWN).

**검증(TDD):** UNKNOWN candidate → 크림 호출 안 됨 + 보류 로그; IN_STOCK+신선 → 교집합 진행;
만료 snapshot → UNKNOWN 취급; OUT_OF_STOCK → drop. `pytest tests/test_runtime_stock_gate.py -v`.
기존 runtime 테스트 통과.

---

## 조각 1c-3: 무신사 재고 API 폴백 수정 (구멍 B)

**Files:** Modify `src/crawlers/musinsa_httpx.py`(`_fetch_inventories_api` 476-525) · Test `tests/test_musinsa.py`

- API 400/타임아웃/스키마이상 → **전 사이즈 IN_STOCK 승격 절대 금지.** `SourceStockSnapshot(UNKNOWN,
  reason_code="api_400"/"timeout")` 반환(해당 상품 이번 사이클 알림 보류).
- `optionItems.activated`: `false`=음성 증거(제외), `true`=선택가능일 뿐 → **UNKNOWN**(양성 승격 X).
  확실히 매핑된 IN_STOCK 사이즈만 통과, 나머지 개별 UNKNOWN.
- 재시도 1회 → 계속 실패 시 상품 보류 → 반복 400이면 소싱처 verifier circuit breaker.

**검증(TDD):** 삼바 JR2660류 재현 — API 400 시 전 사이즈 IN_STOCK 아님(UNKNOWN); activated=true만으론
IN_STOCK 아님. `pytest tests/test_musinsa.py -v`. 기존 통과.

---

## 조각 1c-4: 어댑터 capability 계약 + listing-only JIT verifier (구멍 A, 최대)

**Files:** Create `src/adapters/_stock_capability.py` · Modify listing-only 어댑터(우선 `on_running_adapter.py` 1개 파일럿) · Test

- 어댑터별 capability 3종: `SIZE_STOCK_SUPPORTED`(공식 응답에 사이즈 재고) / `SIZE_STOCK_RESOLVABLE`
  (listing엔 없지만 JIT 상세로 가능) / `SIZE_STOCK_UNOBSERVABLE`(허용 GET 경로에 없음).
- listing-only(available_sizes 안 채우는) 어댑터: RESOLVABLE이면 크림 호출 전 JIT verifier가 상세 조회로
  IN_STOCK 사이즈 확보. UNOBSERVABLE이면 `SourceStockSnapshot(UNKNOWN, reason_code="unsupported")`.
- 계약 테스트(verifier별): 재고상품→정확 사이즈+IN_STOCK / 전품절→OUT_OF_STOCK / 400·타임아웃·파싱실패→
  UNKNOWN / 미지원→UNKNOWN.
- **파일럿 1개(on_running) 먼저 → 검증 → 나머지 어댑터 점진**(이 조각은 파일럿까지, 전체 롤아웃은 후속).

**검증(TDD):** on_running capability 판정 + JIT verifier 계약 테스트. live-tester로 실동작(크림 실제
한글 상품명 색상 교차검증 필수). 기존 on_running 테스트 통과.

---

## 미루는 것 (1c 스코프 밖 — 후속)
- 사이즈별 수량·사이즈별 실결제가 → 별도 "가격 정확성 게이트"(stock state와 분리).
- `SourceStockVerified` 별도 이벤트 분리 → 5단계 아키텍처.
- 1c-4 전체 어댑터 롤아웃 → 파일럿 검증 후 점진.

## 실행 순서
1c-1(모델) → 1c-2(게이트) → 1c-3(무신사) → 1c-4(capability+파일럿). 각 조각 executor 위임 → 머리 검증
→ 커밋. 1c 완료 후 코덱스 verify 1회(적대검증).
