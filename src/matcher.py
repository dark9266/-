"""모델번호 기반 정확 매칭 엔진.

리테일 상품의 모델번호로 크림 상품을 정확히 매칭한다.
유사 상품이 아닌 동일 상품(모델번호 일치)만 매칭한다.
"""

import re

from src.crawlers.kream import kream_crawler
from src.models.database import Database
from src.models.product import KreamProduct, RetailProduct
from src.utils.logging import setup_logger

logger = setup_logger("matcher")


def normalize_model_number(model_number: str) -> str:
    """모델번호 정규화.

    브랜드마다 모델번호 형식이 다르므로 통일된 비교를 위해 정규화한다.
    - 공백, 하이픈 통일
    - 대문자 변환
    - 앞뒤 공백 제거

    Examples:
        "DQ8423-100"  -> "DQ8423-100"
        "dq8423 100"  -> "DQ8423-100"
        "DQ 8423-100" -> "DQ8423-100"
        "FJ4188100"   -> "FJ4188100"   (하이픈 위치 불명확하면 그대로)
    """
    if not model_number:
        return ""

    text = model_number.strip().upper()

    # 공백을 하이픈으로 대체 (연속 공백도 하나로)
    text = re.sub(r"\s+", "-", text)

    # 연속 하이픈 정리
    text = re.sub(r"-+", "-", text)

    # 앞뒤 하이픈 제거
    text = text.strip("-")

    return text


def model_numbers_match(a: str, b: str) -> bool:
    """두 모델번호가 같은 상품을 가리키는지 판별.

    정규화 후 완전 일치를 기본으로 하되,
    구분자(하이픈/공백) 차이는 무시한다.

    Examples:
        ("DQ8423-100", "DQ8423-100") -> True
        ("DQ8423-100", "dq8423 100") -> True
        ("DQ8423-100", "DQ8423-200") -> False
        ("DQ8423-100", "DQ8423")     -> False (부분 일치 불허)
    """
    norm_a = normalize_model_number(a)
    norm_b = normalize_model_number(b)

    if not norm_a or not norm_b:
        return False

    # 1차: 정규화 후 완전 일치
    if norm_a == norm_b:
        return True

    # 2차: 하이픈/구분자 제거 후 비교
    strip_a = re.sub(r"[-\s]", "", norm_a)
    strip_b = re.sub(r"[-\s]", "", norm_b)

    if strip_a == strip_b:
        return True

    return False


async def find_kream_match(
    retail_product: RetailProduct,
    db: Database,
) -> KreamProduct | None:
    """리테일 상품에 매칭되는 크림 상품을 찾는다.

    1. 먼저 DB에서 모델번호로 검색 (이미 수집된 상품)
    2. 없으면 크림에서 검색하여 매칭 시도

    Args:
        retail_product: 리테일 상품 정보 (model_number 필수)
        db: 데이터베이스 인스턴스

    Returns:
        매칭된 KreamProduct, 없으면 None
    """
    model_number = retail_product.model_number
    if not model_number:
        logger.warning("모델번호 없음: %s (%s)", retail_product.name, retail_product.source)
        return None

    normalized = normalize_model_number(model_number)
    logger.info("크림 매칭 시도: %s (정규화: %s)", model_number, normalized)

    # 1단계: DB에서 검색
    row = await db.find_kream_by_model(normalized)
    if row:
        logger.info("DB에서 매칭 발견: %s (product_id: %s)", row["name"], row["product_id"])
        # DB에 있으면 크림에서 최신 가격 수집
        product = await kream_crawler.get_full_product_info(row["product_id"])
        if product:
            return product

    # 정규화 안 한 원본으로도 시도
    if normalized != model_number:
        row = await db.find_kream_by_model(model_number)
        if row:
            product = await kream_crawler.get_full_product_info(row["product_id"])
            if product:
                return product

    # 2단계: 크림에서 검색
    search_results = await kream_crawler.search_product(model_number)

    if not search_results:
        # 하이픈 제거/추가 등 변형으로 재검색
        alt_query = re.sub(r"[-\s]", " ", model_number)
        if alt_query != model_number:
            search_results = await kream_crawler.search_product(alt_query)

    if not search_results:
        logger.info("크림 매칭 실패: %s", model_number)
        return None

    # 검색 결과에서 정확 매칭 찾기 (최대 5건만 상세 조회하여 속도 개선)
    strip_model = re.sub(r"[-\s]", "", normalized).upper()
    checked = 0
    max_detail_checks = 5

    for result in search_results:
        # 조기 필터: 검색 결과 이름에 모델번호 일부가 포함되어 있는지 빠르게 확인
        result_name = result.get("name", "").upper()
        result_name_stripped = re.sub(r"[-\s]", "", result_name)
        # 이름에 모델번호가 전혀 포함되지 않으면 스킵 (완전 무관한 상품)
        if strip_model and len(strip_model) >= 6 and strip_model not in result_name_stripped:
            # 상세 조회 없이 스킵 가능한 명백한 불일치
            pass  # 상세 조회로 넘어감 (이름에 모델번호가 안 들어가는 경우도 있으므로)

        if checked >= max_detail_checks:
            break
        checked += 1

        product = await kream_crawler.get_product_detail(result["product_id"])
        if not product:
            continue

        if model_numbers_match(product.model_number, model_number):
            logger.info(
                "크림 매칭 성공: %s ↔ %s (model: %s)",
                retail_product.name, product.name, product.model_number,
            )

            # DB에 저장
            await db.upsert_kream_product(
                product_id=product.product_id,
                name=product.name,
                model_number=normalize_model_number(product.model_number),
                brand=product.brand,
                image_url=product.image_url,
                url=product.url,
            )

            # 전체 정보 (사이즈별 가격 포함) 수집
            full_product = await kream_crawler.get_full_product_info(product.product_id)
            return full_product

    logger.info(
        "크림 검색 결과 중 정확 매칭 없음: %s (%d건 검색, %d건 상세조회)",
        model_number, len(search_results), checked,
    )
    return None


async def match_and_analyze_batch(
    retail_products: list[RetailProduct],
    db: Database,
) -> list[tuple[RetailProduct, KreamProduct]]:
    """여러 리테일 상품을 일괄 매칭.

    같은 모델번호의 상품은 한 번만 크림 검색한다.

    Returns:
        [(retail_product, kream_product), ...] 매칭된 쌍 목록
    """
    matches: list[tuple[RetailProduct, KreamProduct]] = []
    # 모델번호 → 크림 상품 캐시
    kream_cache: dict[str, KreamProduct | None] = {}

    for rp in retail_products:
        if not rp.model_number:
            continue

        normalized = normalize_model_number(rp.model_number)

        # 캐시 확인
        if normalized in kream_cache:
            kream_product = kream_cache[normalized]
        else:
            kream_product = await find_kream_match(rp, db)
            kream_cache[normalized] = kream_product

        if kream_product:
            matches.append((rp, kream_product))

            # 리테일 상품도 DB에 저장
            await db.upsert_retail_product(
                source=rp.source,
                product_id=rp.product_id,
                name=rp.name,
                model_number=normalized,
                brand=rp.brand,
                url=rp.url,
                image_url=rp.image_url,
            )

    logger.info("일괄 매칭 결과: %d/%d 매칭 성공", len(matches), len(retail_products))
    return matches
