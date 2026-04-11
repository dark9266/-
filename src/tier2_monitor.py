"""Tier 2 실시간 폴링 모니터.

60초 주기로 워치리스트 항목의 크림 시세를 폴링하여
수익 조건 도달 시 알림을 발송한다.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.models.product import AutoScanOpportunity, AutoScanSizeProfit, KreamProduct, Signal
from src.profit_calculator import calculate_kream_fees
from src.utils.logging import setup_logger
from src.watchlist import Watchlist, WatchlistItem

logger = setup_logger("tier2")


@dataclass
class Tier2Result:
    """Tier2 폴링 결과."""

    checked: int = 0
    alerts_sent: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None


class Tier2Monitor:
    """실시간 크림 시세 폴링 모니터."""

    def __init__(
        self,
        watchlist: Watchlist,
        alert_callback: Callable[..., Coroutine[Any, Any, None]] | None = None,
    ):
        self.watchlist = watchlist
        self.alert_callback = alert_callback

    async def run(self) -> Tier2Result:
        """워치리스트 전체 폴링.

        1. 워치리스트 항목별 크림 Pinia API 조회
        2. 사이즈별 수익 계산
        3. 수익 조건 도달 시 알림
        """
        result = Tier2Result()
        items = self.watchlist.get_all()

        if not items:
            result.finished_at = datetime.now()
            return result

        sem = asyncio.Semaphore(min(3, settings.httpx_concurrency))

        async def check_item(item: WatchlistItem) -> None:
            async with sem:
                await self._check_one(item, result)

        await asyncio.gather(
            *[check_item(item) for item in items],
            return_exceptions=True,
        )

        result.finished_at = datetime.now()
        if result.alerts_sent > 0:
            logger.info("Tier2: %d건 체크, %d건 알림", result.checked, result.alerts_sent)
        return result

    async def _check_one(self, item: WatchlistItem, result: Tier2Result) -> None:
        """개별 워치리스트 항목 체크."""
        try:
            result.checked += 1

            # 크림 Pinia API로 최신 시세 조회
            kream_data = await kream_crawler.get_full_product_info(item.kream_product_id)
            if not kream_data:
                return

            sizes = kream_data.get("sizes", [])
            if not sizes:
                return

            # 최저 즉시구매가 업데이트
            buy_prices = [
                s.get("buy_now_price", 0) or s.get("price", 0)
                for s in sizes if (s.get("buy_now_price", 0) or s.get("price", 0)) > 0
            ]
            if buy_prices:
                min_price = min(buy_prices)
                self.watchlist.update_kream_price(item.model_number, min_price)

            # 사이즈별 수익 계산
            best_profit = 0
            best_roi = 0.0
            profitable_sizes: list[AutoScanSizeProfit] = []

            for size_data in sizes:
                kream_price = size_data.get("buy_now_price", 0) or size_data.get("price", 0)
                if not kream_price or kream_price <= 0:
                    continue

                size_name = size_data.get("size", "?")
                fees = calculate_kream_fees(kream_price)
                retail_price = (
                    item.source_price if item.source_price > 0 else item.musinsa_price
                )
                net_profit = kream_price - fees["total_fees"] - retail_price
                roi = (net_profit / retail_price * 100) if retail_price > 0 else 0

                if net_profit > best_profit:
                    best_profit = net_profit
                    best_roi = roi

                if net_profit >= settings.alert_min_profit and roi >= settings.alert_min_roi:
                    profitable_sizes.append(AutoScanSizeProfit(
                        size=str(size_name),
                        musinsa_price=retail_price,
                        kream_bid_price=kream_price,
                        confirmed_profit=net_profit,
                        confirmed_roi=round(roi, 1),
                    ))

            # 수익 조건 도달 시 알림 (Tier2는 가격 기반 필터만 적용)
            if profitable_sizes and self.alert_callback:
                # 순수익 기준으로 시그널 결정 (거래량은 Tier1에서 이미 검증됨)
                if best_profit >= settings.signals.strong_buy_profit:
                    signal = Signal.STRONG_BUY
                elif best_profit >= settings.signals.buy_profit:
                    signal = Signal.BUY
                else:
                    signal = Signal.WATCH

                if signal in (Signal.STRONG_BUY, Signal.BUY):
                    kream_product = KreamProduct(
                        product_id=item.kream_product_id,
                        name=item.kream_name,
                        model_number=item.model_number,
                    )
                    source_url = (
                        item.source_url
                        or f"https://www.musinsa.com/products/{item.musinsa_product_id}"
                    )
                    opportunity = AutoScanOpportunity(
                        kream_product=kream_product,
                        musinsa_url=source_url,
                        musinsa_name="",
                        musinsa_product_id=item.source_product_id or item.musinsa_product_id,
                        size_profits=profitable_sizes,
                        best_confirmed_profit=best_profit,
                        best_confirmed_roi=round(best_roi, 1),
                        signal=signal,
                        volume_7d=1,
                    )
                    try:
                        await self.alert_callback(opportunity)
                        result.alerts_sent += 1
                    except Exception as e:
                        result.errors += 1
                        logger.error("Tier2 알림 발송 실패: %s", e)

        except Exception as e:
            result.errors += 1
            logger.debug("Tier2 체크 실패 (%s): %s", item.model_number, e)
