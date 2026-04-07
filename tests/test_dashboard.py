"""모니터링 대시보드 테스트."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import discord

from src.discord_bot.formatter import (
    format_circuit_breaker_status,
    format_help,
    format_watchlist_embed,
)
from src.scan_cache import ScanCache
from src.watchlist import WatchlistItem


def _make_item(model: str, gap: int, source: str = "musinsa") -> WatchlistItem:
    return WatchlistItem(
        kream_product_id="1",
        model_number=model,
        kream_name=f"테스트 {model}",
        musinsa_product_id="99",
        musinsa_price=89000,
        kream_price=120000,
        gap=gap,
        source=source,
        source_price=120000 + gap,
        source_url="",
    )


class TestFormatWatchlistEmbed:
    """워치리스트 Embed 테스트."""

    def test_creates_embed(self):
        """3개 아이템 → embed 생성."""
        items = [_make_item("A-001", -5000), _make_item("B-002", -10000), _make_item("C-003", -3000)]
        embed = format_watchlist_embed(items)
        assert isinstance(embed, discord.Embed)
        assert "3건" in embed.title

    def test_empty_list(self):
        """빈 리스트 → '비어 있습니다'."""
        embed = format_watchlist_embed([])
        assert "비어 있습니다" in embed.fields[0].value

    def test_truncation(self):
        """15개 아이템 → 10개만, 푸터에 '외 5건'."""
        items = [_make_item(f"M-{i:03d}", -i * 1000) for i in range(15)]
        embed = format_watchlist_embed(items)
        assert "15건" in embed.title
        assert embed.footer.text is not None
        assert "외 5건" in embed.footer.text

    def test_sorted_by_gap(self):
        """gap 내림차순 정렬."""
        items = [
            _make_item("LOW", -30000),
            _make_item("HIGH", 5000),
            _make_item("MID", -10000),
        ]
        embed = format_watchlist_embed(items)
        table_text = embed.fields[0].value
        # HIGH(gap=+5000)가 LOW(gap=-30000)보다 먼저 나와야 함
        high_pos = table_text.index("HIGH")
        low_pos = table_text.index("LOW")
        assert high_pos < low_pos


class TestFormatCircuitBreakerStatus:
    """서킷브레이커 상태 포맷 테스트."""

    def test_formats_status(self):
        """활성/비활성 상태 포맷."""
        status = {"musinsa": "✅ 무신사", "nike": "⏸️ 나이키 (재시도: 14:30)"}
        result = format_circuit_breaker_status(status)
        assert "✅ 무신사" in result
        assert "⏸️ 나이키" in result

    def test_empty_status(self):
        """빈 dict → '등록된 소싱처 없음'."""
        assert format_circuit_breaker_status({}) == "등록된 소싱처 없음"


class TestScanCacheGetStats:
    """스캔 캐시 통계 테스트."""

    def test_get_stats(self):
        """total/profitable 정확도."""
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        tmp = Path(f.name)
        f.close()
        tmp.write_text("{}")

        cache = ScanCache(path=tmp)
        cache.record("MODEL-A", profitable=False)
        cache.record("MODEL-B", profitable=True)
        cache.record("MODEL-C", profitable=True)

        stats = cache.get_stats()
        assert stats["total"] == 3
        assert stats["profitable"] == 2

        tmp.unlink(missing_ok=True)


class TestSchedulerTimestamps:
    """스케줄러 타임스탬프 테스트."""

    def test_initial_none(self):
        """초기값 None."""
        from src.scheduler import Scheduler
        mock_bot = MagicMock()
        scheduler = Scheduler(mock_bot)
        assert scheduler.last_tier1_run is None
        assert scheduler.last_tier2_run is None


class TestCrawlerRegistration:
    """크롤러 레지스트리 등록 테스트."""

    def test_all_crawlers_register(self):
        """4개 소싱처 모듈 임포트 시 레지스트리 등록."""
        from src.crawlers.registry import RETAIL_CRAWLERS
        RETAIL_CRAWLERS.clear()

        import importlib
        import src.crawlers.musinsa_httpx
        import src.crawlers.twentynine_cm
        import src.crawlers.nike
        import src.crawlers.adidas

        # 모듈 리로드로 register() 재실행
        importlib.reload(src.crawlers.musinsa_httpx)
        importlib.reload(src.crawlers.twentynine_cm)
        importlib.reload(src.crawlers.nike)
        importlib.reload(src.crawlers.adidas)

        assert "musinsa" in RETAIL_CRAWLERS
        assert "29cm" in RETAIL_CRAWLERS
        assert "nike" in RETAIL_CRAWLERS
        assert "adidas" in RETAIL_CRAWLERS


class TestFormatHelpNewCommands:
    """도움말에 새 명령어 포함 테스트."""

    def test_has_new_commands(self):
        """!워치리스트, !강제스캔 포함."""
        embed = format_help()
        text = embed.fields[0].value
        assert "!워치리스트" in text
        assert "!강제스캔" in text
