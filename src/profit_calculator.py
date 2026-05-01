"""수익 계산 엔진.

크림 판매가에서 모든 수수료/비용을 차감하여 순수익과 ROI를 계산하고,
시그널을 판정한다.
"""

import math
from dataclasses import dataclass

from src.config import settings
from src.coupon_store import lookup_catch
from src.models.product import (
    AutoScanOpportunity,
    AutoScanSizeProfit,
    KreamProduct,
    KreamSizePrice,
    ProfitOpportunity,
    RetailProduct,
    RetailSizeInfo,
    Signal,
    SizeProfitResult,
)
from src.utils.logging import setup_logger

logger = setup_logger("profit_calc")


def calculate_kream_fees(sell_price: int) -> dict:
    """크림 판매 시 발생하는 수수료 상세 계산.

    Args:
        sell_price: 크림 즉시판매가

    Returns:
        {"sell_fee": int, "inspection_fee": int, "kream_shipping_fee": int,
         "seller_shipping_fee": int, "total_fees": int}
    """
    fees = settings.fees

    # 수수료 = (기본료 + 판매가 × 등급수수료율) × (1 + 부가세)
    fee_subtotal = fees.base_fee + sell_price * fees.sell_fee_rate
    sell_fee = round(fee_subtotal * (1 + fees.vat_rate))

    inspection_fee = fees.inspection_fee  # 0원 (무료)
    kream_shipping_fee = fees.kream_shipping_fee  # 0원 (무료)
    seller_shipping_fee = settings.shipping_cost_to_kream  # 판매자 배송비

    total_fees = sell_fee + inspection_fee + kream_shipping_fee + seller_shipping_fee

    return {
        "sell_fee": sell_fee,
        "inspection_fee": inspection_fee,
        "kream_shipping_fee": kream_shipping_fee,
        "seller_shipping_fee": seller_shipping_fee,
        "total_fees": total_fees,
    }


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

    Args:
        retail_price: 리테일 사이트 구매가 (할인 적용된 실구매가)
        kream_sell_price: 크림 즉시판매가
        in_stock: 해당 사이즈 재고 여부
        bid_count: 구매 입찰 수
        kream_last_sale_price: 마지막 체결가 (등록판매 시 도달 가격, 정보 표시용)
        kream_buy_now_price: 즉시구매가 (등록판매 상한 참고)

    Returns:
        SizeProfitResult
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


def determine_signal(
    net_profit: int,
    volume_7d: int,
) -> Signal:
    """시그널 판정.

    Args:
        net_profit: 순수익 (원)
        volume_7d: 7일 거래량

    Returns:
        Signal enum
    """
    thresholds = settings.signals

    if volume_7d < thresholds.min_volume_7d:
        return Signal.NOT_RECOMMENDED

    if net_profit >= thresholds.strong_buy_profit and volume_7d >= thresholds.strong_buy_volume_7d:
        return Signal.STRONG_BUY

    if net_profit >= thresholds.buy_profit and volume_7d >= thresholds.buy_volume_7d:
        return Signal.BUY

    if net_profit >= thresholds.watch_profit:
        return Signal.WATCH

    return Signal.NOT_RECOMMENDED


def analyze_opportunity(
    kream_product: KreamProduct,
    retail_products: list[RetailProduct],
) -> ProfitOpportunity | None:
    """수익 기회 분석.

    크림 상품과 매칭된 리테일 상품들의 사이즈별 수익을 계산한다.
    각 사이즈에 대해 가장 저렴한 리테일 가격을 기준으로 계산.

    Returns:
        ProfitOpportunity or None (수익 가능한 사이즈가 없으면)
    """
    if not kream_product.size_prices or not retail_products:
        return None

    # 사이즈별 최저 리테일 가격 맵 구축
    retail_size_map: dict[str, tuple[int, bool]] = {}  # size -> (min_price, in_stock)
    for rp in retail_products:
        for rs in rp.sizes:
            if not rs.in_stock:
                continue
            key = _normalize_size(rs.size)
            current = retail_size_map.get(key)
            if current is None or rs.price < current[0]:
                retail_size_map[key] = (rs.price, rs.in_stock)

    # 사이즈별 수익 계산
    size_profits: list[SizeProfitResult] = []

    for ksp in kream_product.size_prices:
        if ksp.sell_now_price is None:
            continue

        kream_size = _normalize_size(ksp.size)
        retail_info = retail_size_map.get(kream_size)

        if retail_info is None:
            continue

        retail_price, in_stock = retail_info
        result = calculate_size_profit(
            retail_price=retail_price,
            kream_sell_price=ksp.sell_now_price,
            in_stock=in_stock,
            bid_count=ksp.bid_count,
        )
        result.size = ksp.size
        result.signal = determine_signal(result.net_profit, kream_product.volume_7d)
        size_profits.append(result)

    if not size_profits:
        return None

    # 최고 수익 사이즈 기준 정보
    best = max(size_profits, key=lambda x: x.net_profit)
    overall_signal = determine_signal(best.net_profit, kream_product.volume_7d)

    opportunity = ProfitOpportunity(
        kream_product=kream_product,
        retail_products=retail_products,
        size_profits=sorted(size_profits, key=lambda x: -x.net_profit),
        best_profit=best.net_profit,
        best_roi=best.roi,
        signal=overall_signal,
    )

    logger.info(
        "수익 분석: %s | 최고 순수익 %s원 (%s%%) | 시그널: %s",
        kream_product.name,
        f"{best.net_profit:,}",
        best.roi,
        overall_signal.value,
    )

    return opportunity


def _normalize_size(size: str) -> str:
    """사이즈 문자열 정규화. '270', '270mm', '27', '27cm' -> '270'."""
    size = size.strip().upper()
    # mm/cm 제거
    size = size.replace("MM", "").replace("CM", "").strip()

    try:
        num = float(size)
        # 30 이하면 cm 단위로 간주 -> mm 변환
        if num <= 35:
            return str(int(num * 10))
        return str(int(num))
    except ValueError:
        # S, M, L 같은 문자 사이즈
        return size


def analyze_auto_scan_opportunity(
    kream_product: KreamProduct,
    musinsa_sizes: dict[str, tuple[int, bool]],
    musinsa_url: str = "",
    musinsa_name: str = "",
    musinsa_product_id: str = "",
) -> AutoScanOpportunity | None:
    """자동스캔 2단계 수익 분석.

    Args:
        kream_product: 크림 상품 (사이즈별 가격, 거래량 포함)
        musinsa_sizes: 무신사 사이즈별 {size: (price, in_stock)}
        musinsa_url: 무신사 구매 링크
        musinsa_name: 무신사 상품명
        musinsa_product_id: 무신사 상품 ID

    Returns:
        AutoScanOpportunity or None
    """
    if not kream_product.size_prices or not musinsa_sizes:
        return None

    # 최근 7일 체결가 평균 계산 (사이즈별 last_sale_price 활용)
    recent_prices: dict[str, int] = {}
    for sp in kream_product.size_prices:
        if sp.last_sale_price:
            recent_prices[_normalize_size(sp.size)] = sp.last_sale_price

    size_profits: list[AutoScanSizeProfit] = []

    for ksp in kream_product.size_prices:
        kream_size = _normalize_size(ksp.size)
        musinsa_info = musinsa_sizes.get(kream_size)
        if not musinsa_info:
            continue

        musinsa_price, in_stock = musinsa_info
        if not in_stock or musinsa_price <= 0:
            continue

        sp = AutoScanSizeProfit(
            size=ksp.size,
            musinsa_price=musinsa_price,
            kream_bid_price=ksp.sell_now_price,
            kream_recent_price=recent_prices.get(kream_size) or ksp.last_sale_price,
            bid_count=ksp.bid_count,
            in_stock=in_stock,
        )

        # [1순위] 확정 수익: 즉시판매가(bid) 기반
        if ksp.sell_now_price and ksp.sell_now_price > 0:
            fees = calculate_kream_fees(ksp.sell_now_price)
            total_cost = musinsa_price + fees["total_fees"]
            sp.confirmed_profit = ksp.sell_now_price - total_cost
            sp.confirmed_roi = round(
                (sp.confirmed_profit / musinsa_price * 100) if musinsa_price > 0 else 0, 1
            )

        # [2순위] 예상 수익: 최근 체결가 기반
        recent_price = sp.kream_recent_price
        if recent_price and recent_price > 0:
            fees = calculate_kream_fees(recent_price)
            total_cost = musinsa_price + fees["total_fees"]
            sp.estimated_profit = recent_price - total_cost
            sp.estimated_roi = round(
                (sp.estimated_profit / musinsa_price * 100) if musinsa_price > 0 else 0, 1
            )

        size_profits.append(sp)

    if not size_profits:
        return None

    # 최고 수익 기준
    best_confirmed = max(size_profits, key=lambda x: x.confirmed_profit)
    best_estimated = max(size_profits, key=lambda x: x.estimated_profit)

    # bid-ask 스프레드 (시세 안정성 지표)
    spreads = []
    for ksp in kream_product.size_prices:
        if ksp.sell_now_price and ksp.last_sale_price:
            spreads.append(abs(ksp.sell_now_price - ksp.last_sale_price))
    avg_spread = int(sum(spreads) / len(spreads)) if spreads else 0

    return AutoScanOpportunity(
        kream_product=kream_product,
        musinsa_url=musinsa_url,
        musinsa_name=musinsa_name,
        musinsa_product_id=musinsa_product_id,
        size_profits=sorted(size_profits, key=lambda x: -x.confirmed_profit),
        best_confirmed_profit=best_confirmed.confirmed_profit,
        best_confirmed_roi=best_confirmed.confirmed_roi,
        best_estimated_profit=best_estimated.estimated_profit,
        best_estimated_roi=best_estimated.estimated_roi,
        volume_7d=kream_product.volume_7d,
        price_trend=kream_product.price_trend,
        bid_ask_spread=avg_spread,
    )


def format_profit_summary(opportunity: ProfitOpportunity) -> str:
    """수익 기회 요약 텍스트 (디버그/로그용)."""
    lines = [
        f"=== {opportunity.kream_product.name} ===",
        f"모델번호: {opportunity.kream_product.model_number}",
        f"시그널: {opportunity.signal.value}",
        f"최고 순수익: {opportunity.best_profit:,}원 (ROI {opportunity.best_roi}%)",
        "",
        "구매처:",
    ]

    for rp in opportunity.retail_products:
        lines.append(f"  - {rp.source}: {rp.url}")

    lines.append("")
    lines.append("사이즈별 수익:")
    for sp in opportunity.size_profits:
        stock = "O" if sp.in_stock else "X"
        lines.append(
            f"  {sp.size:>5s} | 구매 {sp.retail_price:>8,}원 → 판매 {sp.kream_sell_price:>8,}원 "
            f"| 순수익 {sp.net_profit:>7,}원 ({sp.roi:>5.1f}%) [{sp.signal.value}] 재고:{stock}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chrome Extension catch hook — 추가만, 기존 흐름 수정 X (Phase 1.5)
# ---------------------------------------------------------------------------


@dataclass
class CatchAppliedPrice:
    buy_price: int
    catch_applied: bool
    catch_source: str | None = None  # 'checkout' | None


async def apply_catch_to_buy_price(
    *,
    sourcing: str,
    native_id: str,
    color_code: str,
    size_code: str,
    original_price: int,
    db_path: str | None = None,
) -> CatchAppliedPrice:
    """매칭 검증(색·사이즈 일치) 통과한 결제 페이지 catch row 적용.

    PDP 정보는 봇 어댑터가 SSR fetch + 옵션 API 로 수집 중 →
    확장의 PDP catch 는 폐기. 이 hook 은 결제 페이지 catch (page_type='checkout')만 적용.

    - checkout catch + final_price > 0 → final_price 그대로 사용
    - 그 외 (catch 없음, color/size mismatch, page_type='pdp') → original_price 그대로

    매칭 1순위 원칙: catch 데이터는 매칭 후단(가격 계산) 보강일 뿐.
    매칭 단계 (모델번호·색상·사이즈 동일성 검증) 은 호출자가 책임.
    """
    catch = await lookup_catch(
        sourcing, native_id, color_code, size_code, db_path=db_path
    )
    if catch is None or catch.page_type != "checkout":
        return CatchAppliedPrice(buy_price=original_price, catch_applied=False)

    final = catch.payload.get("final_price")
    if isinstance(final, (int, float)) and final > 0:
        # 무신사 결제가는 정수 won. JSON 직렬화 시 174000.0 float 가능 →
        # round() 로 정수화.
        return CatchAppliedPrice(
            buy_price=round(final),
            catch_applied=True,
            catch_source="checkout",
        )

    return CatchAppliedPrice(buy_price=original_price, catch_applied=False)
