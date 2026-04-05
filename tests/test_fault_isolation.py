"""장애 격리 (서킷브레이커) 테스트."""

from datetime import datetime, timedelta

from src.crawlers.registry import (
    RETAIL_CRAWLERS,
    get_active,
    get_status,
    is_active,
    record_failure,
    record_success,
    register,
)


class _FakeCrawler:
    pass


def _reset():
    """테스트 간 레지스트리 초기화."""
    RETAIL_CRAWLERS.clear()


class TestCircuitBreaker:
    """서킷브레이커 장애 격리 테스트."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_consecutive_failures_disables_crawler(self):
        """연속 3회 record_failure() → is_active()=False."""
        register("test", _FakeCrawler(), "테스트")
        record_failure("test")
        record_failure("test")
        assert is_active("test") is True  # 2회까지는 활성
        record_failure("test")
        assert is_active("test") is False  # 3회 → 비활성화

    def test_disabled_crawler_reactivates_after_timeout(self):
        """비활성화 기간 만료 → 재활성화."""
        register("test", _FakeCrawler(), "테스트")
        for _ in range(3):
            record_failure("test")
        assert is_active("test") is False

        # disabled_until을 과거로 조작
        RETAIL_CRAWLERS["test"]["disabled_until"] = datetime.now() - timedelta(minutes=1)
        assert is_active("test") is True
        assert RETAIL_CRAWLERS["test"]["fail_count"] == 0

    def test_success_resets_fail_counter(self):
        """실패 2회 후 성공 → fail_count=0."""
        register("test", _FakeCrawler(), "테스트")
        record_failure("test")
        record_failure("test")
        assert RETAIL_CRAWLERS["test"]["fail_count"] == 2
        record_success("test")
        assert RETAIL_CRAWLERS["test"]["fail_count"] == 0

    def test_all_crawlers_disabled_returns_empty(self):
        """전체 비활성화 시 get_active()={}."""
        register("a", _FakeCrawler(), "A")
        register("b", _FakeCrawler(), "B")
        for key in ["a", "b"]:
            for _ in range(3):
                record_failure(key)
        assert get_active() == {}

    def test_single_crawler_failure_others_continue(self):
        """1개 소싱처 비활성화 → 나머지 정상 반환."""
        register("ok1", _FakeCrawler(), "정상1")
        register("ok2", _FakeCrawler(), "정상2")
        register("fail", _FakeCrawler(), "실패")
        for _ in range(3):
            record_failure("fail")

        active = get_active()
        assert "ok1" in active
        assert "ok2" in active
        assert "fail" not in active

    def test_get_status_shows_disabled(self):
        """get_status()에 비활성 크롤러 표시."""
        register("ok", _FakeCrawler(), "정상")
        register("fail", _FakeCrawler(), "실패")
        for _ in range(3):
            record_failure("fail")

        status = get_status()
        assert "✅" in status["ok"]
        assert "⏸️" in status["fail"]


class TestRegistryIntegration:
    """레지스트리 통합 테스트."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_record_failure_success_cycle(self):
        """실패→비활성화→성공 복원 사이클."""
        register("test", _FakeCrawler(), "테스트")
        for _ in range(3):
            record_failure("test")
        assert is_active("test") is False

        # 시간 경과 시뮬레이션
        RETAIL_CRAWLERS["test"]["disabled_until"] = datetime.now() - timedelta(minutes=1)
        assert is_active("test") is True

        # 성공 기록
        record_success("test")
        assert RETAIL_CRAWLERS["test"]["fail_count"] == 0

    def test_record_failure_unknown_key_no_error(self):
        """존재하지 않는 키에 실패 기록 → 에러 없음."""
        record_failure("nonexistent")
        record_success("nonexistent")
