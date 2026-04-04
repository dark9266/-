"""전체 파이프라인 자동 검증 스크립트.

봇 실행 없이 코드만으로 핵심 로직을 검증한다.
모든 코드 수정 후 커밋 전에 실행할 것.

Usage:
    PYTHONPATH=. python scripts/verify.py
"""

import ast
import inspect
import sys
import textwrap

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        msg = f"{name}: {detail}" if detail else name
        FAILURES.append(msg)
        print(f"  ✗ {name} — {detail}")


# ───────────────────────────────────────────
# 1) 알림 필터 검증
# ───────────────────────────────────────────
def verify_alert_filter():
    print("\n[1] 알림 필터 검증")

    from src.models.product import Signal
    from src.profit_calculator import determine_signal

    # 1-a) 낮은 수익 + 거래량 0 → NOT_RECOMMENDED (알림 스킵 대상)
    sig = determine_signal(782, 0)
    check(
        "determine_signal(782, 0) → 알림 스킵 대상",
        sig not in (Signal.STRONG_BUY, Signal.BUY),
        f"got {sig.value}, expected NOT_RECOMMENDED or WATCH",
    )

    # 1-b) 충분한 수익 + 거래량 → BUY (알림 전송 대상)
    sig2 = determine_signal(15000, 5)
    check(
        "determine_signal(15000, 5) → BUY (알림 전송 대상)",
        sig2 == Signal.BUY,
        f"got {sig2.value}, expected BUY",
    )

    # 1-c) STRONG_BUY 시나리오
    sig3 = determine_signal(30000, 10)
    check(
        "determine_signal(30000, 10) → STRONG_BUY",
        sig3 == Signal.STRONG_BUY,
        f"got {sig3.value}, expected STRONG_BUY",
    )

    # 1-d) on_opportunity 콜백에 signal 체크 분기 존재 확인
    source = open("src/discord_bot/bot.py").read()
    tree = ast.parse(source)

    signal_checks = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            src_segment = ast.get_source_segment(source, node)
            if src_segment and "signal not in" in src_segment:
                signal_checks += 1

    check(
        "on_opportunity 콜백에 signal 체크 분기 존재",
        signal_checks >= 4,
        f"found {signal_checks} signal checks, expected ≥4",
    )


# ───────────────────────────────────────────
# 2) 품절 필터 검증
# ───────────────────────────────────────────
def verify_stock_filter():
    print("\n[2] 품절 필터 검증")

    from src.crawlers.musinsa import MusinsaCrawler

    crawler = MusinsaCrawler.__new__(MusinsaCrawler)

    # 테스트용 options_data: 사이즈 3개 (no=1,2,3)
    options_data = {
        "data": {
            "basic": [
                {
                    "name": "사이즈",
                    "no": 10,
                    "standardOptionNo": 6,
                    "optionValues": [
                        {"no": 1, "name": "250", "isDeleted": False},
                        {"no": 2, "name": "260", "isDeleted": False},
                        {"no": 3, "name": "270", "isDeleted": False},
                    ],
                }
            ],
            "optionItems": [
                {"no": 101, "activated": True, "optionValueNos": [1]},
                {"no": 102, "activated": True, "optionValueNos": [2]},
                {"no": 103, "activated": True, "optionValueNos": [3]},
            ],
        }
    }

    # 2-a) inventory_data=[] (빈 리스트) → 품절 처리 (inventory API 응답 왔지만 데이터 없음)
    sizes_empty_inv = crawler._parse_sizes_from_api(
        options_data=options_data,
        inventory_data=[],
        sale_price=100000,
        original_price=120000,
        discount_type="",
        discount_rate=0,
    )
    check(
        "inventory_data=[] → 0개 사이즈 (전체 품절)",
        len(sizes_empty_inv) == 0,
        f"got {len(sizes_empty_inv)} sizes, expected 0",
    )

    # 2-b) inventory_data=None → isDeleted 폴백 (기존 로직 유지)
    sizes_none_inv = crawler._parse_sizes_from_api(
        options_data=options_data,
        inventory_data=None,
        sale_price=100000,
        original_price=120000,
        discount_type="",
        discount_rate=0,
    )
    check(
        "inventory_data=None → 기존 로직 유지 (isDeleted 폴백, 3개)",
        len(sizes_none_inv) == 3,
        f"got {len(sizes_none_inv)} sizes, expected 3",
    )

    # 2-c) inventory_data에 품절 표시 → 해당 사이즈만 제거
    inv_partial = [
        {
            "outOfStock": True,
            "quantity": 0,
            "relatedOption": {"optionValueNo": 2},
        },
        {
            "outOfStock": False,
            "quantity": 5,
            "relatedOption": {"optionValueNo": 1},
        },
        {
            "outOfStock": False,
            "quantity": 3,
            "relatedOption": {"optionValueNo": 3},
        },
    ]
    sizes_partial = crawler._parse_sizes_from_api(
        options_data=options_data,
        inventory_data=inv_partial,
        sale_price=100000,
        original_price=120000,
        discount_type="",
        discount_rate=0,
    )
    check(
        "inventory 부분 품절 → 2개 사이즈",
        len(sizes_partial) == 2,
        f"got {len(sizes_partial)} sizes, expected 2",
    )


# ───────────────────────────────────────────
# 3) 카테고리 스캔 검증
# ───────────────────────────────────────────
def verify_category_scan():
    print("\n[3] 카테고리 스캔 검증")

    source = open("src/scanner.py").read()

    # 3-a) 브랜드 필터 제거 확인 — 카테고리 스캔 메서드에 brand_filter가 없어야 함
    # run_category_scan 메서드의 "3단계 필터링" 부분에 "브랜드 필터 제거" 코멘트 확인
    check(
        "카테고리스캔 브랜드 필터 제거 확인",
        "브랜드 필터 제거" in source,
        "run_category_scan에 '브랜드 필터 제거' 주석이 없음",
    )

    # 3-b) 이름 매칭 로직 존재 확인
    check(
        "이름 매칭 로직 존재 (name_match_queue)",
        "name_match_queue" in source,
        "name_match_queue가 scanner.py에 없음",
    )

    check(
        "이름 매칭 카운터 존재 (name_matched)",
        "name_matched" in source,
        "name_matched 카운터가 scanner.py에 없음",
    )


# ───────────────────────────────────────────
# 4) 수수료 계산 검증
# ───────────────────────────────────────────
def verify_fee_calculation():
    print("\n[4] 수수료 계산 검증")

    from src.profit_calculator import calculate_kream_fees

    fees = calculate_kream_fees(167000)

    # 수수료 = (2500 + 167000 × 0.06) × 1.1 = (2500 + 10020) × 1.1 = 13772
    expected_sell_fee = round((2500 + 167000 * 0.06) * 1.1)
    check(
        f"판매수수료 = {expected_sell_fee:,}원",
        fees["sell_fee"] == expected_sell_fee,
        f"got {fees['sell_fee']:,}, expected {expected_sell_fee:,}",
    )

    check(
        "검수비 = 0원",
        fees["inspection_fee"] == 0,
        f"got {fees['inspection_fee']}",
    )

    check(
        "배송비 = 3,000원",
        fees["seller_shipping_fee"] == 3000,
        f"got {fees['seller_shipping_fee']}",
    )

    # 정산금 = 판매가 - 총수수료
    settlement = 167000 - fees["total_fees"]
    expected_settlement = 167000 - (expected_sell_fee + 0 + 0 + 3000)
    check(
        f"판매가 167,000원 → 정산 {expected_settlement:,}원",
        settlement == expected_settlement,
        f"got {settlement:,}, expected {expected_settlement:,}",
    )


# ───────────────────────────────────────────
# 메인
# ───────────────────────────────────────────
def main():
    print("=" * 50)
    print("전체 파이프라인 자동 검증")
    print("=" * 50)

    verify_alert_filter()
    verify_stock_filter()
    verify_category_scan()
    verify_fee_calculation()

    print("\n" + "=" * 50)
    if FAIL == 0:
        print(f"ALL PASS ({PASS} checks)")
        print("=" * 50)
        return 0
    else:
        print(f"FAILED: {FAIL}/{PASS + FAIL} checks")
        for f in FAILURES:
            print(f"  → {f}")
        print("=" * 50)
        return 1


if __name__ == "__main__":
    sys.exit(main())
