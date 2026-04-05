"""워치리스트 관리.

Tier1에서 gap 스크리닝한 상품을 저장하고,
Tier2에서 실시간 폴링 대상으로 사용한다.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from src.config import DATA_DIR, settings
from src.utils.logging import setup_logger

logger = setup_logger("watchlist")


@dataclass
class WatchlistItem:
    """워치리스트 항목."""

    kream_product_id: str
    model_number: str
    kream_name: str
    musinsa_product_id: str  # 하위호환 유지
    musinsa_price: int  # 하위호환 유지
    kream_price: int  # 최근 시세
    gap: int  # source_price - kream_price
    added_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_checked: str = field(default_factory=lambda: datetime.now().isoformat())
    # 멀티소스 필드
    source: str = "musinsa"  # 최저가 소싱처 키
    source_product_id: str = ""  # 최저가 소싱처 상품 ID
    source_price: int = 0  # 최저가 가격 (0이면 musinsa_price 사용)
    source_url: str = ""  # 최저가 상품 URL


class Watchlist:
    """watchlist.json 기반 워치리스트."""

    def __init__(self, path: Path | None = None):
        self._path = path or (DATA_DIR / "watchlist.json")
        self._items: dict[str, WatchlistItem] = {}  # key: model_number
        self.load()

    def load(self) -> None:
        """JSON 파일에서 로드."""
        if not self._path.exists():
            self._items = {}
            return

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = {}
            for item_data in data:
                item = WatchlistItem(**item_data)
                # 멀티소스 마이그레이션: source_price 백필
                if item.source_price == 0 and item.musinsa_price > 0:
                    item.source_price = item.musinsa_price
                    item.source_product_id = item.source_product_id or item.musinsa_product_id
                    item.source = item.source or "musinsa"
                self._items[item.model_number] = item
            logger.info("워치리스트 로드: %d건", len(self._items))
        except Exception as e:
            logger.error("워치리스트 로드 실패: %s", e)
            self._items = {}

    def save(self) -> None:
        """JSON 파일에 저장."""
        try:
            data = [asdict(item) for item in self._items.values()]
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("워치리스트 저장 실패: %s", e)

    def add(self, item: WatchlistItem) -> bool:
        """항목 추가. 이미 있으면 업데이트."""
        is_new = item.model_number not in self._items
        self._items[item.model_number] = item
        self.save()
        if is_new:
            logger.debug("워치리스트 추가: %s (gap=%d)", item.model_number, item.gap)
        return is_new

    def remove(self, model_number: str) -> bool:
        """항목 제거."""
        if model_number in self._items:
            del self._items[model_number]
            self.save()
            return True
        return False

    def get_all(self) -> list[WatchlistItem]:
        """전체 항목 반환."""
        return list(self._items.values())

    def get(self, model_number: str) -> WatchlistItem | None:
        """모델번호로 항목 조회."""
        return self._items.get(model_number)

    def update_kream_price(self, model_number: str, new_price: int) -> None:
        """크림 시세 업데이트."""
        item = self._items.get(model_number)
        if item:
            item.kream_price = new_price
            retail_price = item.source_price if item.source_price > 0 else item.musinsa_price
            item.gap = retail_price - new_price
            item.last_checked = datetime.now().isoformat()
            self.save()

    def cleanup_stale(self, max_age_hours: int | None = None) -> int:
        """만료 항목 정리."""
        max_hours = max_age_hours or settings.watchlist_max_age_hours
        cutoff = datetime.now() - timedelta(hours=max_hours)
        stale = [
            mn for mn, item in self._items.items()
            if datetime.fromisoformat(item.added_at) < cutoff
        ]
        for mn in stale:
            del self._items[mn]

        if stale:
            self.save()
            logger.info("워치리스트 만료 정리: %d건 제거", len(stale))

        return len(stale)

    @property
    def size(self) -> int:
        return len(self._items)
