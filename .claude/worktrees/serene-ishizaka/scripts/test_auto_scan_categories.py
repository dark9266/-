"""다중 카테고리 자동스캔 테스트 스크립트.

크림 인기상품을 신발/의류/모자/가방 카테고리에서 골고루 수집하고,
무신사 3단계 검색(복합 모델번호 분리, 상품명 2차, 브랜드+상품명 3차)을 테스트한다.
"""

import asyncio
import logging
import os
import sys

# 프로젝트 루트 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa import musinsa_crawler
from src.matcher import normalize_model_number
from src.models.database import Database
from src.scanner import Scanner
from src.utils.logging import setup_logger

# 로깅 레벨을 DEBUG로 설정 (3단계 검색 로그 확인)
logging.getLogger("scanner").setLevel(logging.DEBUG)
logging.getLogger("kream_crawler").setLevel(logging.INFO)
logging.getLogger("musinsa_crawler").setLevel(logging.INFO)

logger = setup_logger("test_categories")
logger.setLevel(logging.DEBUG)


async def test_kream_collection():
    """카테고리별 크림 인기상품 수집 테스트."""
    logger.info("=" * 60)
    logger.info("1단계: 크림 카테고리별 인기상품 수집 테스트")
    logger.info("=" * 60)

    db = Database()
    await db.connect()
    scanner = Scanner(db)

    # 카테고리별 수집
    categories = ["sneakers", "clothing", "bags", "accessories"]
    products = await scanner._collect_kream_popular(categories=categories)

    logger.info("\n총 수집: %d건", len(products))

    # 카테고리별 통계
    cat_stats: dict[str, list] = {}
    for p in products:
        cat = p.get("_category", "unknown")
        if cat not in cat_stats:
            cat_stats[cat] = []
        cat_stats[cat].append(p)

    for cat, items in sorted(cat_stats.items()):
        logger.info("\n--- %s (%d건) ---", cat.upper(), len(items))
        for i, item in enumerate(items[:3], 1):
            logger.info(
                "  %d. %s | %s | %s",
                i,
                item.get("name", "?")[:40],
                item.get("brand", "?"),
                item.get("model_number", "모델번호 없음"),
            )
        if len(items) > 3:
            logger.info("  ... 외 %d건", len(items) - 3)

    await db.close()
    return products


async def test_auto_scan_10():
    """10개 상품으로 자동스캔 E2E 테스트."""
    logger.info("\n" + "=" * 60)
    logger.info("2단계: 자동스캔 E2E 테스트 (10개 상품)")
    logger.info("=" * 60)

    db = Database()
    await db.connect()
    scanner = Scanner(db)

    # 매칭 검토 콜백 (디스코드 대신 로그로 출력)
    review_logs = []

    async def mock_review_callback(message: str):
        review_logs.append(message)
        logger.info("\n[#수정 채널 기록]\n%s", message)

    scanner._match_review_callback = mock_review_callback

    # 진행 상황 콜백
    async def on_progress(msg: str):
        logger.info("[진행] %s", msg)

    # 수익 기회 콜백
    opportunities_found = []

    async def on_opportunity(opp):
        opportunities_found.append(opp)
        logger.info(
            "[수익 기회] %s | 확정 ROI %.1f%% | 예상 ROI %.1f%%",
            opp.kream_product.name[:30],
            opp.best_confirmed_roi,
            opp.best_estimated_roi,
        )

    # 테스트용으로 max_products=10으로 제한
    original_max = settings.auto_scan_max_products
    settings.auto_scan_max_products = 10

    try:
        result = await scanner.auto_scan(
            on_opportunity=on_opportunity,
            on_progress=on_progress,
        )

        logger.info("\n" + "=" * 60)
        logger.info("자동스캔 결과 요약")
        logger.info("=" * 60)
        elapsed = (result.finished_at - result.started_at).total_seconds() if result.finished_at else 0
        logger.info("소요시간: %.0f초", elapsed)
        logger.info("크림 스캔: %d건", result.kream_scanned)
        logger.info("무신사 검색: %d건", result.musinsa_searched)
        logger.info("매칭 성공: %d건", result.matched)
        logger.info("수익 기회: %d건 (확정 %d / 예상 %d)",
                     len(result.opportunities), result.confirmed_count, result.estimated_count)
        logger.info("에러: %d건", len(result.errors))

        if result.errors:
            logger.info("\n에러 목록:")
            for err in result.errors[:5]:
                logger.info("  - %s", err)

        if result.opportunities:
            logger.info("\n수익 기회 상세:")
            for opp in result.opportunities[:5]:
                logger.info(
                    "  %s | 무신사 %s | 확정수익 %s원 (ROI %.1f%%) | 예상수익 %s원 (ROI %.1f%%)",
                    opp.kream_product.name[:25],
                    opp.musinsa_name[:20],
                    f"{opp.best_confirmed_profit:,}",
                    opp.best_confirmed_roi,
                    f"{opp.best_estimated_profit:,}",
                    opp.best_estimated_roi,
                )

        if review_logs:
            logger.info("\n매칭 검토 기록 (%d건):", len(review_logs))
            for i, log in enumerate(review_logs[:3], 1):
                logger.info("--- 검토 #%d ---\n%s", i, log)
        else:
            logger.info("\n매칭 검토 기록: 0건 (모델번호 불일치 건 없음)")

    finally:
        settings.auto_scan_max_products = original_max
        await db.close()

    return result


async def main():
    logger.info("크림봇 다중 카테고리 자동스캔 테스트 시작")
    logger.info("카테고리: 신발(sneakers), 의류(clothing), 가방(bags), 액세서리/모자(accessories)")
    logger.info("")

    try:
        # 1단계: 카테고리별 수집 테스트
        products = await test_kream_collection()

        if not products:
            logger.error("크림 인기상품 수집 실패! 테스트 중단.")
            return

        # 2단계: 10개로 자동스캔 E2E 테스트
        result = await test_auto_scan_10()

        logger.info("\n" + "=" * 60)
        logger.info("테스트 완료!")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("테스트 중단 (사용자)")
    except Exception as e:
        logger.error("테스트 실패: %s", e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
