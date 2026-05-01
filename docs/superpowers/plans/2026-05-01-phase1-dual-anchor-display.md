# Phase 1 — Dual-Anchor Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 알림 embed 에 즉시판매가 마진 + 체결가 등록 마진 둘 다 표시 — 사용자가 "즉시 던지기 vs 등록 판매" 결정을 1초 안에 내릴 수 있게.

**Architecture:** 시그널 등급·알림 발사 게이트는 즉시판매가(`kream_sell_price`) 기준 그대로 유지 (보수). `SizeProfitResult` 에 체결가 마진 필드만 추가하고 `format_profit_alert` embed 에 "체결가 등록 시" 라인 추가. 자동스캔 경로(`AutoScanSizeProfit` + `format_auto_scan_alert`) 는 이미 dual 계산 구현되어 있어 본 plan 범위 X.

**Tech Stack:** Python 3.12, dataclasses, discord.py, pytest, aiosqlite

**Spec:** `docs/superpowers/specs/2026-04-26-size-trade-margin-signal-design.md` (master `487c570`)

**Worktree:** master 봇 가동 중 (PID 5300). 본 작업은 worktree `.worktrees/phase1-dual-anchor` 에서 진행, 검증 후 master 머지.

---

## File Structure

| 파일 | 변경 종류 | 책임 |
|---|---|---|
| `src/models/product.py:73-89` | Modify | `SizeProfitResult` 에 dual-anchor 필드 4개 추가 |
| `src/profit_calculator.py:59-95` | Modify | `calculate_size_profit` 인자 + 체결가 마진 계산 |
| `src/profit_calculator.py:128-202` | Modify | `analyze_opportunity` 가 last_sale_price 전달 |
| `src/discord_bot/formatter.py:36-134` | Modify | `format_profit_alert` 체결가 라인 추가, `_format_size_table` 그대로 유지 (폭 제한) |
| `tests/test_profit_calculator.py` | Modify | dual-anchor 회귀 테스트 추가 |
| `tests/test_formatter_dual_anchor.py` | Create | embed 체결가 라인 검증 |

**왜 `_format_size_table` 은 변경 X**: 1024자 제한이라 컬럼 추가 시 사이즈 많은 상품에서 잘림. embed 별도 필드 (`체결가 등록 시 마진`) 로 표시.

---

## Task 0: Worktree 생성 + 봇 영향 격리 확인

**Files:**
- Create: `.worktrees/phase1-dual-anchor/` (git worktree)

- [ ] **Step 1: master 봇 가동 상태 확인**

```bash
ps -ef | grep python.*main | grep -v grep
```

Expected: PID 1개 출력 (현재 5300)

- [ ] **Step 2: 깨끗한 working tree 확인**

```bash
git status --short
```

Expected: untracked log 파일만 (`logs/ui_spawn.out` 등). modified 없음.

- [ ] **Step 3: worktree 생성**

```bash
git worktree add .worktrees/phase1-dual-anchor -b feature/phase1-dual-anchor
cd .worktrees/phase1-dual-anchor
```

- [ ] **Step 4: 새 브랜치 가상환경 호환 확인**

```bash
~/kream-venv/bin/python -c "import sys; sys.path.insert(0, '.'); from src.profit_calculator import calculate_size_profit; print('OK')"
```

Expected: `OK`

---

## Task 1: SizeProfitResult dual-anchor 필드 추가

**Files:**
- Modify: `src/models/product.py:73-89`
- Test: `tests/test_profit_calculator.py` (신규 케이스)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_profit_calculator.py` 끝에 추가:

```python
def test_size_profit_result_has_dual_anchor_fields():
    """SizeProfitResult 가 체결가/즉시구매가 dual-anchor 필드 보유."""
    from src.models.product import SizeProfitResult

    result = SizeProfitResult(
        size="270",
        retail_price=80000,
        kream_sell_price=102000,
        sell_fee=9482,
        inspection_fee=0,
        kream_shipping_fee=0,
        seller_shipping_fee=3000,
        total_cost=92482,
        net_profit=9518,
        roi=11.9,
    )
    # 신규 필드 — 기본값 0
    assert result.kream_last_sale_price == 0
    assert result.kream_buy_now_price == 0
    assert result.net_profit_last_sale == 0
    assert result.roi_last_sale == 0.0
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py::test_size_profit_result_has_dual_anchor_fields -v
```

Expected: FAIL — `AttributeError: 'SizeProfitResult' object has no attribute 'kream_last_sale_price'`

- [ ] **Step 3: 모델 필드 추가**

`src/models/product.py:73-89` 의 SizeProfitResult dataclass 변경:

```python
@dataclass
class SizeProfitResult:
    """사이즈별 수익 계산 결과."""

    size: str
    retail_price: int  # 구매가
    kream_sell_price: int  # 크림 즉시판매가 (시그널 게이트 기준)
    sell_fee: int  # 크림 판매 수수료 (부가세 포함)
    inspection_fee: int  # 검수비
    kream_shipping_fee: int  # 크림 배송비
    seller_shipping_fee: int  # 사업자 택배비
    total_cost: int  # 총 비용 (즉시판매가 기준)
    net_profit: int  # 순수익 (즉시판매가 기준, 시그널 게이트)
    roi: float  # 수익률 % (즉시판매가 기준)
    signal: Signal = Signal.NOT_RECOMMENDED
    in_stock: bool = True
    bid_count: int = 0
    # Phase 1 — Dual-Anchor 표시 (시그널 게이트 영향 X)
    kream_last_sale_price: int = 0  # 마지막 체결가 (등록판매 시 도달 가격, 정보 표시용)
    kream_buy_now_price: int = 0  # 즉시구매가 (등록판매 상한 참고)
    net_profit_last_sale: int = 0  # 체결가 등록 시 순수익
    roi_last_sale: float = 0.0  # 체결가 등록 시 ROI %
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py::test_size_profit_result_has_dual_anchor_fields -v
```

Expected: PASS

- [ ] **Step 5: 기존 테스트 회귀 없음 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py -v
```

Expected: ALL PASS (기존 + 신규)

- [ ] **Step 6: 커밋**

```bash
git add src/models/product.py tests/test_profit_calculator.py
git commit -m "feat(model): SizeProfitResult dual-anchor 필드 추가 (Phase 1)"
```

---

## Task 2: calculate_size_profit dual 마진 계산

**Files:**
- Modify: `src/profit_calculator.py:59-95`
- Test: `tests/test_profit_calculator.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_profit_calculator.py` 에 추가:

```python
def test_calculate_size_profit_dual_anchor():
    """체결가 입력 시 net_profit_last_sale 계산."""
    from src.profit_calculator import calculate_size_profit

    result = calculate_size_profit(
        retail_price=80000,
        kream_sell_price=102000,
        kream_last_sale_price=105000,
        kream_buy_now_price=110000,
    )
    # 즉시판매가 기준 (기존)
    assert result.net_profit > 0
    # 체결가 기준 (신규) — last_sale > sell_now 이므로 더 큰 마진
    assert result.net_profit_last_sale > result.net_profit
    assert result.kream_last_sale_price == 105000
    assert result.kream_buy_now_price == 110000


def test_calculate_size_profit_no_last_sale():
    """체결가 0 인 사이즈 → net_profit_last_sale = 0, kream_last_sale_price = 0."""
    from src.profit_calculator import calculate_size_profit

    result = calculate_size_profit(
        retail_price=80000,
        kream_sell_price=102000,
        kream_last_sale_price=0,
    )
    assert result.net_profit > 0  # sell_now 기준은 정상 계산
    assert result.kream_last_sale_price == 0
    assert result.net_profit_last_sale == 0
    assert result.roi_last_sale == 0.0


def test_calculate_size_profit_signal_gate_unchanged():
    """체결가 입력해도 signal 등급은 sell_now 기준 유지 (보수)."""
    from src.profit_calculator import calculate_size_profit, determine_signal

    # sell_now 기준 마진 = +9,518원 (WATCH 미달, 5k 미만은 NOT_RECOMMENDED)
    result_sell_now_low = calculate_size_profit(
        retail_price=92000,
        kream_sell_price=102000,
        kream_last_sale_price=200000,  # 체결가 매우 높음
    )
    # sell_now 기준 net_profit 만 시그널 결정
    sig = determine_signal(result_sell_now_low.net_profit, volume_7d=10)
    # net_profit 이 sell_now 기준이라 5k 이하면 NOT_RECOMMENDED
    # 체결가 200k 마진 X 영향
    assert result_sell_now_low.net_profit_last_sale > result_sell_now_low.net_profit
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py::test_calculate_size_profit_dual_anchor tests/test_profit_calculator.py::test_calculate_size_profit_no_last_sale tests/test_profit_calculator.py::test_calculate_size_profit_signal_gate_unchanged -v
```

Expected: FAIL — `TypeError: calculate_size_profit() got an unexpected keyword argument 'kream_last_sale_price'`

- [ ] **Step 3: calculate_size_profit 수정**

`src/profit_calculator.py:59-95` 의 함수 시그니처 + 본문 변경:

```python
def calculate_size_profit(
    retail_price: int,
    kream_sell_price: int,
    in_stock: bool = True,
    bid_count: int = 0,
    kream_last_sale_price: int = 0,
    kream_buy_now_price: int = 0,
) -> SizeProfitResult:
    """단일 사이즈의 수익 계산.

    시그널 게이트는 sell_now (즉시판매가) 기준 — 보수 정책.
    last_sale (체결가) 는 등록판매 시 도달 가능 마진의 정보 표시용.
    """
    fees = calculate_kream_fees(kream_sell_price)

    total_cost = retail_price + fees["total_fees"]
    net_profit = kream_sell_price - total_cost
    roi = (net_profit / retail_price * 100) if retail_price > 0 else 0.0

    # Phase 1 — 체결가 등록 시 마진 (정보 표시용, 시그널 영향 X)
    net_profit_last_sale = 0
    roi_last_sale = 0.0
    if kream_last_sale_price > 0:
        fees_last = calculate_kream_fees(kream_last_sale_price)
        total_cost_last = retail_price + fees_last["total_fees"]
        net_profit_last_sale = kream_last_sale_price - total_cost_last
        roi_last_sale = (
            (net_profit_last_sale / retail_price * 100) if retail_price > 0 else 0.0
        )

    return SizeProfitResult(
        size="",  # 호출자가 설정
        retail_price=retail_price,
        kream_sell_price=kream_sell_price,
        sell_fee=fees["sell_fee"],
        inspection_fee=fees["inspection_fee"],
        kream_shipping_fee=fees["kream_shipping_fee"],
        seller_shipping_fee=fees["seller_shipping_fee"],
        total_cost=total_cost,
        net_profit=net_profit,
        roi=round(roi, 1),
        in_stock=in_stock,
        bid_count=bid_count,
        kream_last_sale_price=kream_last_sale_price,
        kream_buy_now_price=kream_buy_now_price,
        net_profit_last_sale=net_profit_last_sale,
        roi_last_sale=round(roi_last_sale, 1),
    )
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add src/profit_calculator.py tests/test_profit_calculator.py
git commit -m "feat(profit): calculate_size_profit dual-anchor 마진 계산 (Phase 1)"
```

---

## Task 3: analyze_opportunity 가 last_sale_price 전달

**Files:**
- Modify: `src/profit_calculator.py:128-202`
- Test: `tests/test_profit_calculator.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_profit_calculator.py` 에 추가:

```python
def test_analyze_opportunity_propagates_last_sale():
    """analyze_opportunity 가 KreamSizePrice.last_sale_price 를 SizeProfitResult 까지 전달."""
    from src.profit_calculator import analyze_opportunity
    from src.models.product import (
        KreamProduct, KreamSizePrice, RetailProduct, RetailSizeInfo,
    )

    kp = KreamProduct(
        product_id="123",
        name="Test",
        model_number="TEST-001",
        size_prices=[
            KreamSizePrice(
                size="270",
                sell_now_price=102000,
                last_sale_price=105000,
                buy_now_price=110000,
                bid_count=3,
            ),
        ],
        volume_7d=10,
    )
    rp = RetailProduct(
        source="test",
        product_id="r1",
        name="Test",
        model_number="TEST-001",
        sizes=[RetailSizeInfo(size="270", price=80000, in_stock=True)],
    )

    op = analyze_opportunity(kp, [rp])
    assert op is not None
    assert len(op.size_profits) == 1
    sp = op.size_profits[0]
    assert sp.kream_last_sale_price == 105000
    assert sp.kream_buy_now_price == 110000
    assert sp.net_profit_last_sale > 0
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py::test_analyze_opportunity_propagates_last_sale -v
```

Expected: FAIL — `assert 0 == 105000`

- [ ] **Step 3: analyze_opportunity 수정**

`src/profit_calculator.py:168-173` 의 `calculate_size_profit` 호출 부분 변경:

```python
        retail_price, in_stock = retail_info
        result = calculate_size_profit(
            retail_price=retail_price,
            kream_sell_price=ksp.sell_now_price,
            in_stock=in_stock,
            bid_count=ksp.bid_count,
            kream_last_sale_price=ksp.last_sale_price or 0,
            kream_buy_now_price=ksp.buy_now_price or 0,
        )
        result.size = ksp.size
        result.signal = determine_signal(result.net_profit, kream_product.volume_7d)
        size_profits.append(result)
```

(추가 라인은 `kream_last_sale_price` + `kream_buy_now_price` 두 줄)

- [ ] **Step 4: 테스트 통과 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add src/profit_calculator.py tests/test_profit_calculator.py
git commit -m "feat(profit): analyze_opportunity 가 dual-anchor 가격 전달 (Phase 1)"
```

---

## Task 4: format_profit_alert embed 체결가 라인 추가

**Files:**
- Modify: `src/discord_bot/formatter.py:36-116`
- Create: `tests/test_formatter_dual_anchor.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_formatter_dual_anchor.py` 신규:

```python
"""Phase 1 — format_profit_alert dual-anchor embed 검증."""

from datetime import datetime

from src.discord_bot.formatter import format_profit_alert
from src.models.product import (
    KreamProduct, KreamSizePrice, ProfitOpportunity, RetailProduct,
    RetailSizeInfo, Signal, SizeProfitResult,
)


def _make_opportunity(*, last_sale_price: int = 105000) -> ProfitOpportunity:
    """체결가 105k (sell_now 102k 보다 +3k) 인 표준 케이스."""
    sp = SizeProfitResult(
        size="270",
        retail_price=80000,
        kream_sell_price=102000,
        sell_fee=9482,
        inspection_fee=0,
        kream_shipping_fee=0,
        seller_shipping_fee=3000,
        total_cost=92482,
        net_profit=9518,
        roi=11.9,
        signal=Signal.WATCH,
        kream_last_sale_price=last_sale_price,
        kream_buy_now_price=110000,
        net_profit_last_sale=12260 if last_sale_price > 0 else 0,
        roi_last_sale=15.3 if last_sale_price > 0 else 0.0,
    )
    return ProfitOpportunity(
        kream_product=KreamProduct(
            product_id="123",
            name="Test Shoe",
            model_number="TEST-001",
            volume_7d=10,
            size_prices=[KreamSizePrice(size="270", sell_now_price=102000)],
        ),
        retail_products=[
            RetailProduct(
                source="test",
                product_id="r1",
                name="Test",
                model_number="TEST-001",
                url="https://test.example.com/p/1",
                sizes=[RetailSizeInfo(size="270", price=80000, in_stock=True)],
            ),
        ],
        size_profits=[sp],
        best_profit=9518,
        best_roi=11.9,
        signal=Signal.WATCH,
        detected_at=datetime(2026, 5, 1, 13, 0, 0),
    )


def test_embed_has_dual_anchor_field():
    """체결가 등록 시 마진 정보가 embed 필드에 포함."""
    op = _make_opportunity()
    embed = format_profit_alert(op)

    field_names = [f.name for f in embed.fields]
    assert any("체결가" in n for n in field_names), f"체결가 필드 없음: {field_names}"


def test_embed_dual_anchor_value_format():
    """체결가 라인에 마진 + 가격 둘 다 포함."""
    op = _make_opportunity()
    embed = format_profit_alert(op)

    dual_field = next((f for f in embed.fields if "체결가" in f.name), None)
    assert dual_field is not None
    # 사이즈 + 마진 + 체결가 모두 표시
    assert "270" in dual_field.value
    assert "12,260" in dual_field.value or "+12,260" in dual_field.value
    assert "105,000" in dual_field.value


def test_embed_no_last_sale_shows_na():
    """체결가 0 인 사이즈는 'N/A' 표시."""
    op = _make_opportunity(last_sale_price=0)
    embed = format_profit_alert(op)

    dual_field = next((f for f in embed.fields if "체결가" in f.name), None)
    assert dual_field is not None
    assert "N/A" in dual_field.value
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_formatter_dual_anchor.py -v
```

Expected: FAIL (3건) — `assert any("체결가" in n for n in field_names)` False

- [ ] **Step 3: formatter.py 수정**

`src/discord_bot/formatter.py:88` (사이즈별 수익 필드 직후) 에 신규 필드 삽입:

```python
    embed.add_field(name="사이즈별 수익", value=size_lines, inline=False)

    # Phase 1 — 체결가 등록 시 마진 (정보 표시, 시그널 영향 X)
    dual_lines = _format_dual_anchor_lines(opportunity.size_profits)
    if dual_lines:
        embed.add_field(name="체결가 등록 시 마진", value=dual_lines, inline=False)
```

같은 파일 `_format_size_table` 함수 직후에 헬퍼 추가:

```python
def _format_dual_anchor_lines(size_profits: list[SizeProfitResult]) -> str:
    """체결가 등록 시 마진 라인 (사이즈별)."""
    lines = ["```"]
    lines.append(f"{'사이즈':>5} {'체결가':>9} {'마진':>9} {'ROI':>6}")
    lines.append("-" * 35)
    for sp in size_profits[:10]:  # 1024자 제한 보호 (상위 10개)
        if sp.kream_last_sale_price > 0:
            lines.append(
                f"{sp.size:>5} {sp.kream_last_sale_price:>8,} "
                f"{sp.net_profit_last_sale:>+8,} {sp.roi_last_sale:>5.1f}%"
            )
        else:
            lines.append(f"{sp.size:>5} {'N/A':>9} {'N/A':>9} {'-':>6}")
    lines.append("```")
    return "\n".join(lines)
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
~/kream-venv/bin/python -m pytest tests/test_formatter_dual_anchor.py -v
```

Expected: 3 PASS

- [ ] **Step 5: 기존 formatter 테스트 회귀 없음**

```bash
~/kream-venv/bin/python -m pytest tests/ -k formatter -v
```

Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add src/discord_bot/formatter.py tests/test_formatter_dual_anchor.py
git commit -m "feat(embed): format_profit_alert 체결가 등록 시 마진 라인 추가 (Phase 1)"
```

---

## Task 5: 시그널 게이트 보존 + canary 회귀 검증

**Files:** (수정 없음, 검증 단계)

- [ ] **Step 1: 전체 pytest**

```bash
~/kream-venv/bin/python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: 1051+ PASS (신규 테스트 포함)

- [ ] **Step 2: verify.py 파이프라인 검증**

```bash
PYTHONPATH=. ~/kream-venv/bin/python scripts/verify.py 2>&1 | tail -10
```

Expected: 62/63 (사전 fail `inventory_data=None` 무관, 우리 변경과 별개)

- [ ] **Step 3: canary 20개 PASS**

```bash
PYTHONPATH=. ~/kream-venv/bin/python scripts/run_canary.py 2>&1 | tail -25
```

Expected: `[canary] PASS 20/20`

- [ ] **Step 4: 시그널 등급 보존 검증 (수동)**

`tests/test_profit_calculator.py` 에 회귀 테스트 추가:

```python
def test_signal_gate_unchanged_with_dual_anchor():
    """체결가 매우 높아도 sell_now 기준 NOT_RECOMMENDED 면 결과 유지."""
    from src.profit_calculator import calculate_size_profit, determine_signal

    # sell_now 기준 net_profit < 5k → NOT_RECOMMENDED
    result = calculate_size_profit(
        retail_price=95000,
        kream_sell_price=102000,
        kream_last_sale_price=300000,  # 체결가 비현실적으로 높음
    )
    sig = determine_signal(result.net_profit, volume_7d=10)
    assert sig.name == "NOT_RECOMMENDED"
    # 체결가 기준으로는 마진 큼 — 단 시그널은 영향 X (보수)
    assert result.net_profit_last_sale > 100000
```

```bash
~/kream-venv/bin/python -m pytest tests/test_profit_calculator.py::test_signal_gate_unchanged_with_dual_anchor -v
```

Expected: PASS

- [ ] **Step 5: 커밋 (회귀 테스트 추가만)**

```bash
git add tests/test_profit_calculator.py
git commit -m "test: dual-anchor 후 시그널 게이트 보존 회귀 테스트 (Phase 1)"
```

---

## Task 6: profit-analyzer 에이전트 의무 검증

**Files:** (검증 단계, 코드 수정 X)

- [ ] **Step 1: profit-analyzer 에이전트 호출**

CLAUDE.md 룰: "수익 계산 로직 변경 (`profit_calculator.py`, 수수료, 시그널 기준) → `profit-analyzer` 검증 필수".

호출 컨텍스트:
- spec: `docs/superpowers/specs/2026-04-26-size-trade-margin-signal-design.md`
- 변경: `src/profit_calculator.py` (calculate_size_profit dual-anchor)
- 변경: `src/models/product.py` (SizeProfitResult 4 필드)
- 변경: `src/discord_bot/formatter.py` (embed 체결가 라인)

검증 요청:
1. 수수료 공식 (`calculate_kream_fees`) 변동 X 확인
2. 시그널 등급 (`determine_signal`) 입력 변동 X 확인 (= sell_now net_profit 기준)
3. 알림 하드 플로어 (10k/5%/vol≥1) 변동 X 확인
4. dual-anchor 마진 계산 산수 (수동 검증 케이스 1건)

- [ ] **Step 2: APPROVED 응답 확인**

profit-analyzer 가 4개 모두 NO 차이 + dual-anchor 산수 정확 확인 시 다음 진행.

REJECTED 시 fix → 재검증.

---

## Task 7: master 머지 + 봇 재시작 + 라이브 검증

**Files:** (배포 단계)

- [ ] **Step 1: worktree 에서 master 로 머지 (사용자 컨펌 후)**

```bash
cd ../..  # 메인 repo 로
git merge feature/phase1-dual-anchor --no-ff -m "Merge: Phase 1 dual-anchor display"
```

- [ ] **Step 2: master 에서 다시 한 번 verify + canary**

```bash
PYTHONPATH=. ~/kream-venv/bin/python scripts/verify.py 2>&1 | tail -5
PYTHONPATH=. ~/kream-venv/bin/python scripts/run_canary.py 2>&1 | tail -5
```

Expected: 62+/63 + canary 20/20

- [ ] **Step 3: 봇 정지 (사용자 컨펌)**

```bash
kill $(cat data/kreambot.pid)
sleep 5
ps -ef | grep python.*main | grep -v grep
```

Expected: 빈 출력

- [ ] **Step 4: 봇 재가동**

```bash
PYTHONPATH=. ~/kream-venv/bin/python main.py >> logs/bot.out 2>&1 &
sleep 10
ps -ef | grep python.*main | grep -v grep
```

Expected: 새 PID 출력

- [ ] **Step 5: 봇 로그 헬스체크**

```bash
tail -20 logs/bot.out
```

확인 항목:
- `V3Runtime 기동 완료`
- `adapters=21`
- 어댑터 첫 호출 성공

- [ ] **Step 6: 라이브 알림 1건 embed 검증**

알림 발생 대기 (10~30분 예상). 수신 시 embed 에 다음 확인:
- `사이즈별 수익` 필드 (기존)
- `체결가 등록 시 마진` 필드 (신규) — 사이즈별 체결가/마진/ROI 또는 N/A

- [ ] **Step 7: worktree 정리**

```bash
git worktree remove .worktrees/phase1-dual-anchor
git branch -d feature/phase1-dual-anchor
```

- [ ] **Step 8: 메모리 갱신**

`/home/wonhee/.claude/projects/-mnt-c-Users-USER-Desktop----/memory/project_size_trade_margin_progress.md` 갱신:
- Phase 1 완료 (커밋 SHA 기록)
- Phase 2/3 별도 plan 대기 상태로 마킹

---

## Spec Coverage 자체 검증

| Spec 요구 | Plan 매핑 |
|---|---|
| Phase 1 Dual-Anchor Display | Task 1~4 (모델 + 계산 + 전달 + embed) |
| 알림 발사 게이트 변경 X | Task 5 step 4 (회귀 테스트) |
| 체결가 N/A 처리 | Task 4 step 1 (test_embed_no_last_sale_shows_na) |
| 시그널 등급 sell_now 기준 유지 | Task 5 step 4 (test_signal_gate_unchanged) |
| canary 20개 PASS | Task 5 step 3 |
| profit-analyzer 의무 검증 | Task 6 |
| 라이브 검증 (알림 1건) | Task 7 step 6 |

Phase 2 (별점), Phase 3 (dual-scenario 정가/보정가) = 본 plan 범위 외, 별도 plan.

## 명시적 미포함 (Phase 2/3 영역)

- 추천 별점 (`src/recommendation.py` 신규) — Phase 2
- 자동 할인 매칭 (`source_discount_constants` config) — Phase 3
- OR 게이트 (`net_profit_nominal OR net_profit_adjusted`) — Phase 3
- `_format_size_table` 컬럼 확장 — 1024자 제한으로 보류, embed 별도 필드로 해결

## 커밋 6개 예정

1. `feat(model): SizeProfitResult dual-anchor 필드 추가 (Phase 1)`
2. `feat(profit): calculate_size_profit dual-anchor 마진 계산 (Phase 1)`
3. `feat(profit): analyze_opportunity 가 dual-anchor 가격 전달 (Phase 1)`
4. `feat(embed): format_profit_alert 체결가 등록 시 마진 라인 추가 (Phase 1)`
5. `test: dual-anchor 후 시그널 게이트 보존 회귀 테스트 (Phase 1)`
6. master 머지 commit (`Merge: Phase 1 dual-anchor display`)
