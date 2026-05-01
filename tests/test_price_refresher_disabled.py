"""price_refresher 비활성화 토글 검증.

INVARIANT: 푸시 단일 트랙 + 캡 초과 주범 → price_refresher 기본 OFF (2026-05-01).
"""

import inspect

from src.config import Settings
from src.scheduler import Scheduler


def test_price_refresher_disabled_by_default():
    s = Settings()
    assert s.price_refresher_enabled is False


def test_refresh_loop_has_disabled_guard():
    src = inspect.getsource(Scheduler.refresh_loop.coro)
    assert "settings.price_refresher_enabled" in src
    guard_idx = src.index("settings.price_refresher_enabled")
    return_idx = src.index("return", guard_idx)
    assert return_idx - guard_idx < 80
