"""멀티소스 워치리스트 통합 테스트."""

import json
import tempfile
from pathlib import Path

from src.watchlist import Watchlist, WatchlistItem


class TestWatchlistMultiSource:
    """워치리스트 멀티소스 필드 테스트."""

    def _make_wl(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp = Path(f.name)
        f.close()
        return Watchlist(path=self._tmp)

    def teardown_method(self):
        if hasattr(self, "_tmp"):
            self._tmp.unlink(missing_ok=True)

    def test_watchlist_load_old_format(self):
        """source 필드 없는 기존 JSON 정상 로드."""
        old_data = [
            {
                "kream_product_id": "1",
                "model_number": "DQ8423-100",
                "kream_name": "덩크 로우",
                "musinsa_product_id": "99",
                "musinsa_price": 89000,
                "kream_price": 120000,
                "gap": -31000,
                "added_at": "2025-01-01T00:00:00",
                "last_checked": "2025-01-01T00:00:00",
            }
        ]
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self._tmp = Path(f.name)
        json.dump(old_data, f, ensure_ascii=False)
        f.close()

        wl = Watchlist(path=self._tmp)
        assert wl.size == 1
        item = wl.get("DQ8423-100")
        assert item is not None
        assert item.source == "musinsa"
        assert item.source_price == 89000  # 백필됨

    def test_watchlist_save_new_format(self):
        """새 필드 포함 저장 확인."""
        wl = self._make_wl()
        wl.add(WatchlistItem(
            kream_product_id="1",
            model_number="TEST-001",
            kream_name="테스트",
            musinsa_product_id="99",
            musinsa_price=89000,
            kream_price=120000,
            gap=-31000,
            source="nike",
            source_product_id="DQ8423-100",
            source_price=85000,
            source_url="https://www.nike.com/kr/t/_/DQ8423-100",
        ))

        raw = json.loads(self._tmp.read_text(encoding="utf-8"))
        assert raw[0]["source"] == "nike"
        assert raw[0]["source_price"] == 85000
        assert raw[0]["source_url"] == "https://www.nike.com/kr/t/_/DQ8423-100"

    def test_watchlist_migration_backfill(self):
        """source_price=0 → musinsa_price 백필."""
        old_data = [
            {
                "kream_product_id": "1",
                "model_number": "BF-001",
                "kream_name": "백필 테스트",
                "musinsa_product_id": "55",
                "musinsa_price": 95000,
                "kream_price": 130000,
                "gap": -35000,
                "added_at": "2025-01-01T00:00:00",
                "last_checked": "2025-01-01T00:00:00",
                "source": "musinsa",
                "source_product_id": "",
                "source_price": 0,
                "source_url": "",
            }
        ]
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self._tmp = Path(f.name)
        json.dump(old_data, f, ensure_ascii=False)
        f.close()

        wl = Watchlist(path=self._tmp)
        item = wl.get("BF-001")
        assert item.source_price == 95000  # musinsa_price로 백필
        assert item.source_product_id == "55"  # musinsa_product_id로 백필

    def test_watchlist_update_uses_source_price(self):
        """gap 계산에 source_price 사용."""
        wl = self._make_wl()
        wl.add(WatchlistItem(
            kream_product_id="1",
            model_number="GAP-001",
            kream_name="갭 테스트",
            musinsa_product_id="99",
            musinsa_price=89000,
            kream_price=120000,
            gap=-31000,
            source="nike",
            source_price=85000,
        ))

        wl.update_kream_price("GAP-001", 130000)
        item = wl.get("GAP-001")
        assert item.kream_price == 130000
        assert item.gap == 85000 - 130000  # source_price 기반

    def test_watchlist_multi_source_items(self):
        """서로 다른 source 항목 공존."""
        wl = self._make_wl()

        wl.add(WatchlistItem(
            kream_product_id="1",
            model_number="A-001",
            kream_name="무신사 상품",
            musinsa_product_id="10",
            musinsa_price=89000,
            kream_price=120000,
            gap=-31000,
            source="musinsa",
            source_price=89000,
        ))
        wl.add(WatchlistItem(
            kream_product_id="2",
            model_number="B-002",
            kream_name="나이키 상품",
            musinsa_product_id="",
            musinsa_price=0,
            kream_price=150000,
            gap=-60000,
            source="nike",
            source_product_id="DQ8423-100",
            source_price=90000,
        ))
        wl.add(WatchlistItem(
            kream_product_id="3",
            model_number="C-003",
            kream_name="29CM 상품",
            musinsa_product_id="",
            musinsa_price=0,
            kream_price=110000,
            gap=-25000,
            source="29cm",
            source_product_id="456",
            source_price=85000,
        ))

        assert wl.size == 3
        assert wl.get("A-001").source == "musinsa"
        assert wl.get("B-002").source == "nike"
        assert wl.get("C-003").source == "29cm"
