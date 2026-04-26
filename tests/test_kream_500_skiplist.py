"""크림 크롤러 500 스킵리스트 단위 테스트.

Phase B3 (2026-04-13): 동일 엔드포인트가 반복 500 반환 시 cap 낭비를
막기 위해 in-memory blacklist 로 TTL 내 즉시 drop 하는 동작 검증.
"""

from __future__ import annotations

import time

from src.crawlers import kream as kream_mod


def setup_function() -> None:
    kream_mod._clear_500_blacklist()


def test_not_blacklisted_initially():
    assert kream_mod._is_500_blacklisted("/products/1") is False


def test_threshold_triggers_blacklist():
    ep = "/products/123"
    for _ in range(kream_mod._500_FAILURE_THRESHOLD):
        kream_mod._record_500_failure(ep)
    assert kream_mod._is_500_blacklisted(ep) is True


def test_below_threshold_not_blacklisted():
    ep = "/products/456"
    for _ in range(kream_mod._500_FAILURE_THRESHOLD - 1):
        kream_mod._record_500_failure(ep)
    assert kream_mod._is_500_blacklisted(ep) is False


def test_ttl_expires_old_failures():
    ep = "/products/789"
    now = time.monotonic()
    # 과거 TTL 초과 실패들 주입
    old = now - kream_mod._500_BLACKLIST_TTL_SEC - 10
    kream_mod._500_failures[ep] = [old, old, old]
    assert kream_mod._is_500_blacklisted(ep, now=now) is False
    # 만료된 항목은 정리되어야 함
    assert kream_mod._500_failures.get(ep) == []


def test_mixed_fresh_and_expired_counts_fresh_only():
    ep = "/products/mixed"
    now = time.monotonic()
    old = now - kream_mod._500_BLACKLIST_TTL_SEC - 10
    # threshold 미만 fresh 만 (threshold-1 개) — old 는 만료라 카운트 X
    fresh_count = kream_mod._500_FAILURE_THRESHOLD - 1
    fresh = [now - (i + 1) for i in range(fresh_count)]
    kream_mod._500_failures[ep] = [old, old] + fresh
    assert kream_mod._is_500_blacklisted(ep, now=now) is False
    # 여기에 fresh 1회 더 추가하면 threshold 도달
    kream_mod._record_500_failure(ep, now=now)
    assert kream_mod._is_500_blacklisted(ep, now=now) is True


def test_different_endpoints_isolated():
    for _ in range(kream_mod._500_FAILURE_THRESHOLD):
        kream_mod._record_500_failure("/products/a")
    assert kream_mod._is_500_blacklisted("/products/a") is True
    assert kream_mod._is_500_blacklisted("/products/b") is False
