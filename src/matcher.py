"""모델번호 기반 정확 매칭 엔진.

리테일 상품의 모델번호로 크림 상품을 정확히 매칭한다.
유사 상품이 아닌 동일 상품(모델번호 일치)만 매칭한다.
"""

import re

from src.crawlers.kream import kream_crawler
from src.models.database import Database
from src.models.product import KreamProduct, KreamSizePrice, RetailProduct
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


def extract_model_from_name(name: str) -> str | None:
    """상품명에서 모델번호 패턴 추출 (상세 페이지 방문 없이).

    카테고리 리스팅 API의 goodsName에서 모델번호를 추출하여
    상세 페이지 방문 없이 크림 DB 매칭을 시도할 때 사용.

    Examples:
        "나이키 덩크 로우 DQ8423-100" -> "DQ8423-100"
        "뉴발란스 574 U7408PL" -> "U7408PL"
        "아디다스 삼바 OG B75806" -> "B75806"
    """
    if not name:
        return None

    text = name.strip().upper()

    # "/" 뒤의 모델번호 (예: "덩크 로우 W - 세일:화이트 / IO4244-100")
    m = re.search(r"/\s*([A-Za-z0-9][-A-Za-z0-9]+)", text)
    if m:
        candidate = m.group(1).strip()
        if re.match(r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}", candidate):
            return normalize_model_number(candidate)
        # ASICS: 숫자시작 (1203A879-021)
        if re.match(r"\d{4}[A-Z]\d{3,4}-\d{3}", candidate):
            return normalize_model_number(candidate)

    # ASICS: 숫자4자리+영문1자리+숫자3~4자리-숫자3자리 (1203A879-021, 1201A256-105)
    m = re.search(r"\b(\d{4}[A-Z]\d{3,4}-\d{3})\b", text)
    if m:
        return normalize_model_number(m.group(1))

    # ASICS (구형): 영문2자리+숫자1+영문1+숫자1+영문1-숫자3 (TH7S2N-100)
    m = re.search(r"\b([A-Z]{2}\d[A-Z]\d[A-Z]-\d{3})\b", text)
    if m:
        return normalize_model_number(m.group(1))

    # Nike/Jordan: XX1234-123
    m = re.search(r"\b([A-Z]{1,3}\d{3,5}-\d{2,4})\b", text)
    if m:
        return normalize_model_number(m.group(1))

    # Adidas: 6자리-3자리 (123456-001)
    m = re.search(r"\b(\d{6}-\d{3})\b", text)
    if m:
        return normalize_model_number(m.group(1))

    # NB/기타: 영문1~2자+숫자3~5자+영문0~3자 (U7408PL, MT410GC5, BB550)
    m = re.search(r"\b([A-Z]{1,2}\d{3,5}[A-Z]{0,3}\d{0,2})\b", text)
    if m:
        candidate = m.group(1)
        if len(candidate) >= 5:
            return normalize_model_number(candidate)

    return None


def _row_to_kream_product(row) -> KreamProduct:
    """DB 행을 KreamProduct 객체로 변환."""
    return KreamProduct(
        product_id=row["product_id"],
        name=row["name"],
        model_number=row["model_number"],
        brand=row["brand"] or "",
        category=row["category"] or "sneakers",
        image_url=row["image_url"] or "",
        url=row["url"] or "",
    )


# 콜라보 키워드 및 검증 로직은 src/core/matching_guards.py 로 이관됨 (Phase 2 공통화).
# 하위 호환 re-export — 기존 import 경로 유지용.
from src.core.matching_guards import COLLAB_KEYWORDS as _COLLAB_KEYWORDS  # noqa: E402
from src.core.matching_guards import collab_match_fails, is_collab  # noqa: E402


def _warn_collab_mismatch(retail_name: str, kream_name: str) -> bool:
    """리테일=일반 + 크림=콜라보 불일치 경고. 불일치면 True 반환."""
    if collab_match_fails(kream_name, retail_name):
        logger.warning(
            "콜라보 불일치: retail='%s' vs kream='%s' — 오매칭 가능성",
            retail_name[:40], kream_name[:40],
        )
        return True
    return False


def _pick_best_kream_match(rows: list, retail_name: str = ""):
    """복수 크림 매칭 중 최적 상품 선택. 콜라보보다 일반 상품 우선."""
    if len(rows) <= 1:
        row = rows[0] if rows else None
        if row and retail_name:
            _warn_collab_mismatch(retail_name, row["name"] or "")
        return row

    normal = []
    collab = []
    for row in rows:
        if is_collab(row["name"] or ""):
            collab.append(row)
        else:
            normal.append(row)

    if normal:
        if len(normal) > 1:
            logger.info(
                "복수 일반 매칭 %d건 중 첫 번째 선택: %s",
                len(normal), normal[0]["name"],
            )
        return normal[0]

    # 전부 콜라보인데 retail은 일반 → 경고
    if retail_name:
        _warn_collab_mismatch(retail_name, collab[0]["name"] if collab else "")

    # 리테일 이름과 가장 유사한 것 선택
    if retail_name and len(collab) > 1:
        retail_lower = retail_name.lower()
        for c in collab:
            kream_lower = (c["name"] or "").lower()
            if any(w in kream_lower for w in retail_lower.split() if len(w) > 3):
                return c

    logger.info(
        "복수 매칭 전부 콜라보 (%d건), 첫 번째 선택: %s",
        len(collab), collab[0]["name"] if collab else "?",
    )
    return (collab or rows)[0]


async def _find_in_db(
    normalized: str, original: str, db: Database, retail_name: str = "",
):
    """DB에서 모델번호로 크림 상품을 검색한다.

    정확 검색 + LIKE 검색 결과를 합산하여 콜라보 필터로 최적 선택.
    예: CW2288-111 → 정확(트래비스 1건) + LIKE(일반AF1 + 트래비스) → 합산 2건 → 일반 선택
    """
    # 1) 정확 검색
    exact_rows = list(await db.find_kream_all_by_model(normalized))

    # 2) 원본으로 정확 검색
    if normalized != original:
        exact_rows += list(await db.find_kream_all_by_model(original))

    # 3) LIKE 검색 (슬래시 구분 모델번호 대응: "315122-111/CW2288-111")
    like_rows = await db.search_kream_all_by_model_like(normalized)

    # 중복 제거 후 합산
    seen_ids: set[str] = set()
    all_rows = []
    for row in exact_rows + list(like_rows):
        pid = row["product_id"]
        if pid not in seen_ids:
            seen_ids.add(pid)
            all_rows.append(row)

    if not all_rows:
        return None

    if len(all_rows) > 1:
        logger.info("복수 크림 매칭 발견: model=%s (%d건)", normalized, len(all_rows))

    return _pick_best_kream_match(all_rows, retail_name)


async def find_kream_match(
    retail_product: RetailProduct,
    db: Database,
) -> KreamProduct | None:
    """리테일 상품에 매칭되는 크림 상품을 찾는다.

    1. 먼저 DB에서 모델번호로 검색 (47,000+ 상품 DB 활용, API 호출 없음)
    2. 없으면 크림 API에서 검색하여 매칭 시도

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

    # 1단계: DB에서 검색 (API 호출 없이 즉시 반환)
    row = await _find_in_db(normalized, model_number, db, retail_name=retail_product.name)
    if row:
        logger.info("DB에서 매칭 발견: %s (product_id: %s)", row["name"], row["product_id"])
        return _row_to_kream_product(row)

    # 2단계: 크림 API에서 검색 (DB에 없을 때만)
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
