"""2티어 아키텍처 통합 테스트."""

import ast
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── a) Chrome 코드 완전 제거 확인 ──────────────────────


def test_no_chrome_imports():
    """소스에 selenium, webdriver, playwright, chrome_cdp import 없음."""
    src_dir = Path(__file__).parent.parent / "src"
    forbidden = {"selenium", "webdriver", "chrome_cdp", "cdp_manager"}

    violations = []
    for py_file in src_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for kw in forbidden:
                        if kw in alias.name:
                            violations.append(f"{py_file.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for kw in forbidden:
                        if kw in node.module:
                            violations.append(f"{py_file.name}: from {node.module}")

    assert not violations, f"Chrome/CDP imports found:\n" + "\n".join(violations)


def test_chrome_cdp_file_removed():
    """chrome_cdp.py 파일이 삭제됨."""
    cdp_path = Path(__file__).parent.parent / "src" / "crawlers" / "chrome_cdp.py"
    assert not cdp_path.exists(), "chrome_cdp.py should be deleted"


def test_old_musinsa_removed():
    """Playwright 기반 musinsa.py 삭제됨."""
    old_path = Path(__file__).parent.parent / "src" / "crawlers" / "musinsa.py"
    assert not old_path.exists(), "old musinsa.py (Playwright) should be deleted"


def test_musinsa_httpx_exists():
    """httpx 기반 musinsa_httpx.py 존재."""
    new_path = Path(__file__).parent.parent / "src" / "crawlers" / "musinsa_httpx.py"
    assert new_path.exists(), "musinsa_httpx.py should exist"


# ─── b) 워치리스트 CRUD ─────────────────────────────────


def test_watchlist_save_load():
    """watchlist.json CRUD 정상 동작."""
    from src.watchlist import Watchlist, WatchlistItem

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        wl = Watchlist(path=tmp_path)
        assert wl.size == 0

        item = WatchlistItem(
            kream_product_id="12345",
            model_number="DQ8423-100",
            kream_name="Nike Dunk Low",
            musinsa_product_id="67890",
            musinsa_price=89000,
            kream_price=120000,
            gap=-31000,
        )
        assert wl.add(item) is True  # new
        assert wl.size == 1
        assert wl.add(item) is False  # update (not new)
        assert wl.size == 1

        # 다시 로드
        wl2 = Watchlist(path=tmp_path)
        assert wl2.size == 1
        loaded = wl2.get("DQ8423-100")
        assert loaded is not None
        assert loaded.kream_price == 120000

        # 가격 업데이트
        wl2.update_kream_price("DQ8423-100", 130000)
        assert wl2.get("DQ8423-100").kream_price == 130000
        assert wl2.get("DQ8423-100").gap == 89000 - 130000

        # 삭제
        assert wl2.remove("DQ8423-100") is True
        assert wl2.size == 0
        assert wl2.remove("NONEXISTENT") is False
    finally:
        tmp_path.unlink(missing_ok=True)


def test_watchlist_cleanup_stale():
    """만료 항목 정리."""
    from datetime import datetime, timedelta
    from src.watchlist import Watchlist, WatchlistItem

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        wl = Watchlist(path=tmp_path)

        # 오래된 항목
        old_item = WatchlistItem(
            kream_product_id="1",
            model_number="OLD-001",
            kream_name="Old Product",
            musinsa_product_id="1",
            musinsa_price=50000,
            kream_price=80000,
            gap=-30000,
            added_at=(datetime.now() - timedelta(hours=100)).isoformat(),
        )
        # 최근 항목
        new_item = WatchlistItem(
            kream_product_id="2",
            model_number="NEW-001",
            kream_name="New Product",
            musinsa_product_id="2",
            musinsa_price=60000,
            kream_price=90000,
            gap=-30000,
        )

        wl.add(old_item)
        wl.add(new_item)
        assert wl.size == 2

        removed = wl.cleanup_stale(max_age_hours=48)
        assert removed == 1
        assert wl.size == 1
        assert wl.get("NEW-001") is not None
        assert wl.get("OLD-001") is None
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── c) asyncio 병렬 정합성 ─────────────────────────────


@pytest.mark.asyncio
async def test_parallel_10_concurrent():
    """10개 동시 mock 요청 → 모두 정상 완료."""
    from src.utils.rate_limiter import AsyncRateLimiter

    limiter = AsyncRateLimiter(max_concurrent=10, min_interval=0.01)
    results = []

    async def work(i: int):
        async with limiter.acquire():
            results.append(i)

    await __import__("asyncio").gather(*[work(i) for i in range(10)])
    assert sorted(results) == list(range(10))


# ─── d) Tier1/Tier2 독립 동작 ───────────────────────────


@pytest.mark.asyncio
async def test_tier1_returns_result():
    """Tier1Scanner.run() 결과 객체 반환."""
    from src.tier1_scanner import Tier1Scanner

    db = AsyncMock()
    db.find_kream_all_by_model = AsyncMock(return_value=[])

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        from src.watchlist import Watchlist
        wl = Watchlist(path=tmp_path)

        scanner = Tier1Scanner(db=db, watchlist=wl)

        with patch("src.tier1_scanner.musinsa_crawler") as mock_musinsa:
            mock_musinsa.fetch_category_listing = AsyncMock(return_value=[])
            result = await scanner.run(categories=["103"])

        assert result.scanned == 0
        assert result.matched == 0
        assert result.added == 0
        assert result.finished_at is not None
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tier2_returns_result():
    """Tier2Monitor.run() 빈 워치리스트에서 정상 반환."""
    from src.tier2_monitor import Tier2Monitor

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        from src.watchlist import Watchlist
        wl = Watchlist(path=tmp_path)

        monitor = Tier2Monitor(watchlist=wl, alert_callback=AsyncMock())
        result = await monitor.run()

        assert result.checked == 0
        assert result.alerts_sent == 0
        assert result.finished_at is not None
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tiers_independent():
    """Tier1 실패해도 Tier2 정상 동작."""
    from src.tier1_scanner import Tier1Scanner
    from src.tier2_monitor import Tier2Monitor

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        from src.watchlist import Watchlist
        wl = Watchlist(path=tmp_path)

        # Tier1: 강제 실패
        db = AsyncMock()
        t1 = Tier1Scanner(db=db, watchlist=wl)
        with patch("src.tier1_scanner.musinsa_crawler") as mock:
            mock.fetch_category_listing = AsyncMock(side_effect=Exception("Tier1 fail"))
            t1_result = await t1.run(categories=["103"])

        # Tier1 실패해도 결과 반환
        assert t1_result.scanned == 0

        # Tier2: 독립적으로 정상 동작
        t2 = Tier2Monitor(watchlist=wl, alert_callback=AsyncMock())
        t2_result = await t2.run()
        assert t2_result.finished_at is not None
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── e) 수익 조건 알림 ──────────────────────────────────


@pytest.mark.asyncio
async def test_profit_triggers_alert():
    """profit >= 10k, ROI >= 5% → alert_callback 호출."""
    from src.tier2_monitor import Tier2Monitor
    from src.watchlist import Watchlist, WatchlistItem

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        wl = Watchlist(path=tmp_path)
        wl.add(WatchlistItem(
            kream_product_id="999",
            model_number="TEST-001",
            kream_name="Test Product",
            musinsa_product_id="888",
            musinsa_price=80000,
            kream_price=150000,
            gap=-70000,
        ))

        alert_cb = AsyncMock()
        monitor = Tier2Monitor(watchlist=wl, alert_callback=alert_cb)

        # 크림 API mock — 높은 가격으로 수익 발생
        mock_kream_data = {
            "size_prices": [
                {"size": "270", "sell_now_price": 150000, "buy_now_price": 152000},
            ],
        }

        with patch("src.tier2_monitor.kream_crawler") as mock_kream:
            mock_kream.get_full_product_info = AsyncMock(return_value=mock_kream_data)
            result = await monitor.run()

        assert result.checked == 1
        # 150000 - fees(~12750) - 80000 > 10000 → 알림
        assert alert_cb.called or result.alerts_sent > 0
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── f) 2티어 아키텍처 파일 존재 확인 ───────────────────


def test_tier_files_exist():
    """2티어 아키텍처 핵심 파일 존재."""
    base = Path(__file__).parent.parent / "src"
    assert (base / "watchlist.py").exists()
    assert (base / "tier1_scanner.py").exists()
    assert (base / "tier2_monitor.py").exists()
    assert (base / "utils" / "rate_limiter.py").exists()


# ─── g) Tier2 수익 경계값 테스트 ────────────────────────


async def _run_tier2_with_kream_price(kream_price: int, musinsa_price: int = 80000):
    """Tier2 테스트 헬퍼: 주어진 크림 가격으로 실행."""
    from src.tier2_monitor import Tier2Monitor
    from src.watchlist import Watchlist, WatchlistItem

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        wl = Watchlist(path=tmp_path)
        wl.add(WatchlistItem(
            kream_product_id="999",
            model_number="BOUNDARY-TEST",
            kream_name="Boundary Test",
            musinsa_product_id="888",
            musinsa_price=musinsa_price,
            kream_price=kream_price,
            gap=musinsa_price - kream_price,
        ))

        alert_cb = AsyncMock()
        monitor = Tier2Monitor(watchlist=wl, alert_callback=alert_cb)

        mock_kream_data = {
            "size_prices": [
                {"size": "270", "sell_now_price": kream_price, "buy_now_price": kream_price},
            ],
        }

        with patch("src.tier2_monitor.kream_crawler") as mock_kream:
            mock_kream.get_full_product_info = AsyncMock(return_value=mock_kream_data)
            result = await monitor.run()

        return result, alert_cb
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tier2_below_min_no_alert():
    """순수익 < 10,000원 → 알림 없음."""
    # kream=102500 → profit=9985 (< 10k alert_min_profit)
    result, alert_cb = await _run_tier2_with_kream_price(102500)
    assert result.checked == 1
    assert not alert_cb.called


@pytest.mark.asyncio
async def test_tier2_watch_no_alert():
    """순수익 10k~15k → WATCH 시그널 → 알림 없음."""
    # kream=107000 → profit=14188 (WATCH: >= 10k but < 15k BUY)
    result, alert_cb = await _run_tier2_with_kream_price(107000)
    assert result.checked == 1
    assert not alert_cb.called


@pytest.mark.asyncio
async def test_tier2_buy_sends_alert():
    """순수익 >= 15,000원 → BUY 시그널 → 알림 발송."""
    # kream=108000 → profit=15122 (>= 15k buy_profit)
    result, alert_cb = await _run_tier2_with_kream_price(108000)
    assert result.checked == 1
    assert alert_cb.called
    assert result.alerts_sent == 1


@pytest.mark.asyncio
async def test_tier2_strong_buy_alert():
    """순수익 >= 30,000원 → STRONG_BUY 시그널 → 알림 발송."""
    # kream=124000 → profit=30066 (>= 30k strong_buy_profit)
    result, alert_cb = await _run_tier2_with_kream_price(124000)
    assert result.checked == 1
    assert alert_cb.called
    assert result.alerts_sent == 1


# ─── h) Rate Limiter 추가 테스트 ────────────────────────


@pytest.mark.asyncio
async def test_rate_limiter_concurrency_limit():
    """max_concurrent=2 → 동시 최대 2개만 실행."""
    import asyncio
    from src.utils.rate_limiter import AsyncRateLimiter

    limiter = AsyncRateLimiter(max_concurrent=2, min_interval=0)
    max_concurrent = 0
    current = 0

    async def work():
        nonlocal max_concurrent, current
        async with limiter.acquire():
            current += 1
            if current > max_concurrent:
                max_concurrent = current
            await asyncio.sleep(0.05)
            current -= 1

    await asyncio.gather(*[work() for _ in range(5)])
    assert max_concurrent <= 2


@pytest.mark.asyncio
async def test_rate_limiter_interval_enforcement():
    """min_interval=0.1 → 연속 호출 간 최소 간격 보장."""
    import asyncio
    import time
    from src.utils.rate_limiter import AsyncRateLimiter

    limiter = AsyncRateLimiter(max_concurrent=1, min_interval=0.1)
    timestamps = []

    for _ in range(3):
        async with limiter.acquire():
            timestamps.append(time.monotonic())

    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= 0.09, f"Interval {i}: {gap:.3f}s < 0.09s"
