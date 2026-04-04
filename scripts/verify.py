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


class MockRow(dict):
    """sqlite3.Row를 흉내내는 딕셔너리. 없는 키는 빈 문자열 반환."""
    def __getitem__(self, key):
        return self.get(key, "")


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
        "on_opportunity 콜백 + 배치루프 signal 체크 ≥6곳",
        signal_checks >= 6,
        f"found {signal_checks} signal checks, expected ≥6",
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

    # 3-a) 스마트 브랜드 필터 확인 — 크림 DB 기반 브랜드 필터
    check(
        "카테고리스캔 스마트 브랜드 필터 확인",
        "kream_brand_slugs" in source,
        "run_category_scan에 크림 브랜드 셋 기반 필터가 없음",
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

    # 3-d) embed에 alert_sent 파라미터 존재 확인
    fmt_source = open("src/discord_bot/formatter.py").read()
    check(
        "format_category_scan_summary에 alert_sent 파라미터",
        "alert_sent" in fmt_source,
        "formatter.py에 alert_sent 파라미터 없음",
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
# 5) 초기화 명령어 검증
# ───────────────────────────────────────────
def verify_init_command():
    print("\n[5] 초기화 명령어 검증")

    source = open("src/discord_bot/bot.py").read()

    # "초기화" 파싱 후 early return 존재 확인
    check(
        "카테고리스캔 초기화 시 early return 존재",
        "if not resume:" in source and "초기화" in source,
        "초기화 분기 후 early return 로직이 없음",
    )

    check(
        "초기화 완료 메시지 존재",
        "초기화되었습니다" in source or "초기화 완료" in source,
        "초기화 완료 메시지가 없음",
    )


# ───────────────────────────────────────────
# 6) 콜라보 매칭 검증
# ───────────────────────────────────────────
def verify_collab_matching():
    print("\n[6] 콜라보 매칭 검증")

    from src.matcher import _COLLAB_KEYWORDS, _pick_best_kream_match, _warn_collab_mismatch

    # 콜라보 키워드 존재 확인
    check(
        "콜라보 키워드 세트 존재 (≥10개)",
        len(_COLLAB_KEYWORDS) >= 10,
        f"got {len(_COLLAB_KEYWORDS)} keywords, expected ≥10",
    )

    # mock rows: 일반 + 콜라보
    normal_row = MockRow({"name": "Nike Air Force 1 07", "product_id": "1"})
    collab_row = MockRow(
        {"name": "Nike x Travis Scott Air Force 1 Cactus Jack", "product_id": "2"}
    )

    # 6-a) 일반+콜라보 → 일반 우선
    result = _pick_best_kream_match([collab_row, normal_row])
    check(
        "일반+콜라보 → 일반 상품 우선 선택",
        result["product_id"] == "1",
        f"got product_id={result['product_id']}, expected '1' (normal)",
    )

    # 6-b) 단일 결과 → 그대로 반환
    result2 = _pick_best_kream_match([collab_row])
    check(
        "단일 결과 → 그대로 반환",
        result2["product_id"] == "2",
        f"got product_id={result2['product_id']}, expected '2'",
    )

    # 6-c) find_kream_all_by_model 메서드 존재 확인
    source = open("src/models/database.py").read()
    check(
        "find_kream_all_by_model 메서드 존재",
        "find_kream_all_by_model" in source,
        "database.py에 find_kream_all_by_model 없음",
    )

    # 6-d) 콜라보 불일치 경고 함수 정상 동작
    _warn_collab_mismatch("에어 포스 1 07 화이트", "나이키 x 트래비스 스캇 AF1")
    check("콜라보 불일치 경고 함수 정상 동작", True, "")

    # 6-e) 한국어 콜라보 키워드 포함
    check(
        "한국어 콜라보 키워드 포함 (트래비스 스캇, 유토피아)",
        "트래비스 스캇" in _COLLAB_KEYWORDS and "유토피아" in _COLLAB_KEYWORDS,
        "한국어 키워드 누락",
    )

    # 6-f) 유토피아 에디션 vs 일반 AF1 선택
    utopia_row = MockRow(
        {"name": "나이키 x 트래비스 스캇 에어포스 1 유토피아", "product_id": "156663"}
    )
    normal_af1 = MockRow(
        {"name": "나이키 에어포스 1 '07 로우 화이트", "product_id": "12831"}
    )
    result3 = _pick_best_kream_match(
        [utopia_row, normal_af1], "에어 포스 1 07 M 화이트"
    )
    check(
        "유토피아 vs 일반 AF1 → 일반 선택",
        result3["product_id"] == "12831",
        f"got {result3['product_id']}, expected 12831",
    )

    # 6-g) 정확+LIKE 합산 시나리오 (CW2288-111 실제 DB 구조)
    # 정확 검색: 트래비스만 매칭 (model=CW2288-111)
    # LIKE 검색: 일반AF1도 매칭 (model=315122-111/CW2288-111)
    # 합산 후 콜라보 필터 → 일반 선택
    exact_only = MockRow(
        {"name": "나이키 x 트래비스 스캇 에어포스 1 캑터스 잭 유토피아", "product_id": "156663"}
    )
    slash_model = MockRow(
        {"name": "나이키 에어포스 1 '07 로우 화이트", "product_id": "12831"}
    )
    combined = _pick_best_kream_match(
        [exact_only, slash_model], "에어 포스 1 07 M 화이트"
    )
    check(
        "정확+LIKE 합산 → 일반 AF1(12831) 선택",
        combined["product_id"] == "12831",
        f"got {combined['product_id']}, expected 12831",
    )


# ───────────────────────────────────────────
# 7) Chrome SSH 폴백 검증
# ───────────────────────────────────────────
def verify_chrome_ssh_fallback():
    print("\n[7] Chrome SSH 폴백 검증")

    source = open("src/crawlers/chrome_cdp.py").read()

    check(
        "shutil.which 폴백 로직 존재",
        "shutil.which" in source,
        "chrome_cdp.py에 shutil.which 없음",
    )

    check(
        "pkill 폴백 존재 (WSL 네이티브)",
        "pkill" in source,
        "chrome_cdp.py에 pkill 폴백 없음",
    )

    check(
        "import shutil 존재",
        "import shutil" in source,
        "chrome_cdp.py에 shutil import 없음",
    )


# ───────────────────────────────────────────
# [8] 빠른테스트 데이터 흐름 검증
# ───────────────────────────────────────────
def verify_quick_test_data_flow():
    print("\n[8] 빠른테스트 데이터 흐름 검증")

    from src.matcher import _row_to_kream_product

    # 8-a) _row_to_kream_product는 size_prices 없음 확인
    mock_row = MockRow({
        "product_id": "12831", "name": "테스트", "model_number": "CW2288-111",
        "brand": "Nike", "category": "sneakers", "image_url": "", "url": "",
    })
    basic_product = _row_to_kream_product(mock_row)
    check(
        "DB-only KreamProduct에 size_prices 없음",
        len(basic_product.size_prices) == 0,
        f"expected 0, got {len(basic_product.size_prices)}",
    )
    check(
        "DB-only KreamProduct에 volume_7d=0",
        basic_product.volume_7d == 0,
        f"expected 0, got {basic_product.volume_7d}",
    )

    # 8-b) quick_test에서 _get_kream_with_cache 호출 경로 확인
    src = inspect.getsource(__import__("src.scanner", fromlist=["Scanner"]).Scanner.quick_test)
    check(
        "quick_test에 _get_kream_with_cache 호출 존재",
        "_get_kream_with_cache" in src,
        "find_kream_match 후 풀 데이터 가져오기 로직 누락",
    )
    check(
        "quick_test에 size_prices 확인 분기 존재",
        "size_prices" in src,
        "size_prices 비어있을 때 풀 데이터 가져오기 분기 누락",
    )


# ───────────────────────────────────────────
# [9] 카테고리스캔 브랜드 필터 검증
# ───────────────────────────────────────────
def verify_brand_filter():
    print("\n[9] 카테고리스캔 브랜드 필터 검증")
    import re

    # 9-a) get_all_kream_brand_slugs 메서드 존재
    from src.models.database import Database
    check(
        "get_all_kream_brand_slugs 메서드 존재",
        hasattr(Database, "get_all_kream_brand_slugs"),
        "database.py에 메서드 누락",
    )

    # 9-b) 브랜드 정규화 로직 검증
    def normalize_brand(b: str) -> str:
        return re.sub(r"[^a-z0-9]", "", b.lower())

    check(
        "정규화: 'new balance' → 'newbalance'",
        normalize_brand("new balance") == "newbalance",
        f"got {normalize_brand('new balance')}",
    )
    arcteryx_result = normalize_brand("arc'teryx")
    check(
        "정규화: 'arc'teryx' → 'arcteryx'",
        arcteryx_result == "arcteryx",
        f"got {arcteryx_result}",
    )
    check(
        "정규화: 'the north face' → 'thenorthface'",
        normalize_brand("the north face") == "thenorthface",
        f"got {normalize_brand('the north face')}",
    )
    check(
        "정규화: 'carhartt wip' → 'carhartwip' (동일 slug)",
        normalize_brand("carhartt wip") == "carharttwip",
        f"got {normalize_brand('carhartt wip')}",
    )

    # 9-c) scanner.py에 브랜드 필터 로직 존재
    src = inspect.getsource(
        __import__("src.scanner", fromlist=["Scanner"]).Scanner.run_category_scan
    )
    check(
        "카테고리스캔에 kream_brand_slugs 로드 존재",
        "kream_brand_slugs" in src,
        "브랜드 셋 로드 로직 누락",
    )
    check(
        "카테고리스캔에 brand_filtered 카운트 존재",
        "brand_filtered" in src,
        "브랜드 필터 카운트 누락",
    )

    # 9-d) 병렬 상세 방문 구조 확인
    check(
        "카테고리스캔에 asyncio.gather 병렬 처리 존재",
        "asyncio.gather" in src,
        "병렬 상세 방문 로직 누락",
    )
    check(
        "카테고리스캔에 Semaphore 동시 제한 존재",
        "Semaphore" in src,
        "동시 접속 제한 누락 (무신사 차단 위험)",
    )
    check(
        "카테고리스캔에 return_exceptions=True 존재",
        "return_exceptions=True" in src,
        "gather 에러 격리 미적용",
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
    verify_init_command()
    verify_collab_matching()
    verify_chrome_ssh_fallback()
    verify_quick_test_data_flow()
    verify_brand_filter()

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
