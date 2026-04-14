"""가격 변동 감지 모듈.

이전 가격과 현재 가격을 비교하여 의미 있는 변동을 감지하고 기록한다.
"""

from dataclasses import dataclass
from datetime import datetime

from src.models.database import Database
from src.models.product import KreamProduct, RetailProduct, Signal
from src.profit_calculator import calculate_size_profit, determine_signal
from src.utils.logging import setup_logger

logger = setup_logger("price_tracker")


@dataclass
class PriceChange:
    """가격 변동 정보."""

    product_name: str
    model_number: str
    kream_product_id: str
    change_source: str  # "kream" or "retail"
    size: str
    old_price: int
    new_price: int
    price_diff: int  # 양수=가격 상승, 음수=가격 하락
    old_profit: int
    new_profit: int
    profit_diff: int
    old_signal: Signal
    new_signal: Signal
    detected_at: datetime


class PriceTracker:
    """가격 변동 추적기."""

    def __init__(self, db: Database):
        self.db = db

    async def check_kream_price_changes(
        self,
        kream_product: KreamProduct,
        retail_price_map: dict[str, int] | None = None,
    ) -> list[PriceChange]:
        """크림 가격 변동 감지.

        이전에 저장된 가격과 현재 가격을 비교한다.

        Args:
            kream_product: 최신 크림 상품 정보
            retail_price_map: {size: retail_price} 리테일 가격 맵 (수익 변동 계산용)

        Returns:
            감지된 가격 변동 목록
        """
        changes: list[PriceChange] = []

        # 이전 가격 조회
        prev_prices = await self.db.get_latest_kream_prices(kream_product.product_id)
        if not prev_prices:
            # 최초 수집이면 변동 없음, 현재 가격만 저장
            await self._save_current_kream_prices(kream_product)
            return changes

        # 이전 가격 맵 구축
        prev_map: dict[str, dict] = {}
        for row in prev_prices:
            prev_map[row["size"]] = {
                "sell_now_price": row["sell_now_price"],
                "buy_now_price": row["buy_now_price"],
            }

        # 사이즈별 비교
        for sp in kream_product.size_prices:
            if sp.sell_now_price is None:
                continue

            prev = prev_map.get(sp.size)
            if not prev or prev["sell_now_price"] is None:
                continue

            old_sell = prev["sell_now_price"]
            new_sell = sp.sell_now_price
            diff = new_sell - old_sell

            # 의미 있는 변동만 (1,000원 이상 또는 1% 이상)
            if abs(diff) < 1000 and (old_sell == 0 or abs(diff) / old_sell < 0.01):
                continue

            # 수익 변동 계산
            old_profit = 0
            new_profit = 0
            old_signal = Signal.NOT_RECOMMENDED
            new_signal = Signal.NOT_RECOMMENDED

            if retail_price_map and sp.size in retail_price_map:
                retail_price = retail_price_map[sp.size]
                old_result = calculate_size_profit(retail_price, old_sell)
                new_result = calculate_size_profit(retail_price, new_sell)
                old_profit = old_result.net_profit
                new_profit = new_result.net_profit
                old_signal = determine_signal(old_profit, kream_product.volume_7d)
                new_signal = determine_signal(new_profit, kream_product.volume_7d)

            change = PriceChange(
                product_name=kream_product.name,
                model_number=kream_product.model_number,
                kream_product_id=kream_product.product_id,
                change_source="kream",
                size=sp.size,
                old_price=old_sell,
                new_price=new_sell,
                price_diff=diff,
                old_profit=old_profit,
                new_profit=new_profit,
                profit_diff=new_profit - old_profit,
                old_signal=old_signal,
                new_signal=new_signal,
                detected_at=datetime.now(),
            )
            changes.append(change)

            logger.info(
                "크림 가격 변동: %s [%s] %s원 → %s원 (%+d원) | 순수익 %+d원",
                kream_product.name, sp.size,
                f"{old_sell:,}", f"{new_sell:,}", diff,
                new_profit - old_profit,
            )

        # 현재 가격 저장
        await self._save_current_kream_prices(kream_product)

        return changes

    async def check_retail_price_changes(
        self,
        retail_product: RetailProduct,
        kream_sell_map: dict[str, int] | None = None,
        volume_7d: int = 0,
    ) -> list[PriceChange]:
        """리테일 가격 변동 감지.

        Args:
            retail_product: 최신 리테일 상품 정보
            kream_sell_map: {size: kream_sell_price} 크림 가격 맵
            volume_7d: 7일 거래량
        """
        changes: list[PriceChange] = []

        # 이전 리테일 가격 조회
        cursor = await self.db.db.execute(
            """SELECT size, price FROM retail_price_history
            WHERE source = ? AND product_id = ?
            AND fetched_at = (
                SELECT MAX(fetched_at) FROM retail_price_history
                WHERE source = ? AND product_id = ?
            )""",
            (retail_product.source, retail_product.product_id,
             retail_product.source, retail_product.product_id),
        )
        prev_rows = await cursor.fetchall()

        if not prev_rows:
            # 최초 수집
            await self._save_current_retail_prices(retail_product)
            return changes

        prev_map = {row["size"]: row["price"] for row in prev_rows}

        for size_info in retail_product.sizes:
            if size_info.size not in prev_map:
                continue

            old_price = prev_map[size_info.size]
            new_price = size_info.price
            diff = new_price - old_price

            if abs(diff) < 1000 and (old_price == 0 or abs(diff) / old_price < 0.01):
                continue

            # 수익 변동 계산
            old_profit = 0
            new_profit = 0
            old_signal = Signal.NOT_RECOMMENDED
            new_signal = Signal.NOT_RECOMMENDED

            if kream_sell_map and size_info.size in kream_sell_map:
                kream_price = kream_sell_map[size_info.size]
                old_result = calculate_size_profit(old_price, kream_price)
                new_result = calculate_size_profit(new_price, kream_price)
                old_profit = old_result.net_profit
                new_profit = new_result.net_profit
                old_signal = determine_signal(old_profit, volume_7d)
                new_signal = determine_signal(new_profit, volume_7d)

            change = PriceChange(
                product_name=retail_product.name,
                model_number=retail_product.model_number,
                kream_product_id="",
                change_source=retail_product.source,
                size=size_info.size,
                old_price=old_price,
                new_price=new_price,
                price_diff=diff,
                old_profit=old_profit,
                new_profit=new_profit,
                profit_diff=new_profit - old_profit,
                old_signal=old_signal,
                new_signal=new_signal,
                detected_at=datetime.now(),
            )
            changes.append(change)

            logger.info(
                "리테일 가격 변동: %s(%s) [%s] %s원 → %s원 (%+d원)",
                retail_product.name, retail_product.source, size_info.size,
                f"{old_price:,}", f"{new_price:,}", diff,
            )

        await self._save_current_retail_prices(retail_product)

        return changes

    def filter_significant_changes(
        self, changes: list[PriceChange], min_profit_diff: int = 3000
    ) -> list[PriceChange]:
        """알림할 가치가 있는 변동만 필터링.

        조건:
        - 순수익 변동이 min_profit_diff 이상
        - 또는 시그널이 변경됨
        """
        significant = []
        for c in changes:
            if abs(c.profit_diff) >= min_profit_diff:
                significant.append(c)
            elif c.old_signal != c.new_signal:
                significant.append(c)
        return significant

    async def _save_current_kream_prices(self, kream_product: KreamProduct) -> None:
        """크림 현재 가격을 이력에 저장."""
        price_data = []
        for sp in kream_product.size_prices:
            price_data.append({
                "size": sp.size,
                "sell_now_price": sp.sell_now_price,
                "buy_now_price": sp.buy_now_price,
                "bid_count": sp.bid_count,
                "last_sale_price": sp.last_sale_price,
            })
        if price_data:
            await self.db.save_kream_prices(kream_product.product_id, price_data)

    async def _save_current_retail_prices(self, retail_product: RetailProduct) -> None:
        """리테일 현재 가격을 이력에 저장."""
        for si in retail_product.sizes:
            await self.db.db.execute(
                """INSERT INTO retail_price_history
                (source, product_id, size, price, original_price, in_stock, discount_type, discount_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    retail_product.source, retail_product.product_id,
                    si.size, si.price, si.original_price,
                    1 if si.in_stock else 0,
                    si.discount_type, si.discount_rate,
                ),
            )
        await self.db.db.commit()
