"""에러 처리 및 자동 복구 유틸리티.

크롤링 재시도, Chrome 자동 복구, 글로벌 에러 핸들링.
"""

import asyncio
import functools
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta

from src.utils.logging import setup_logger

logger = setup_logger("resilience")


def retry(
    max_attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: Callable | None = None,
):
    """비동기 함수 재시도 데코레이터.

    Args:
        max_attempts: 최대 시도 횟수
        delay: 초기 대기 시간 (초)
        backoff: 대기 시간 배수
        exceptions: 재시도할 예외 튜플
        on_retry: 재시도 시 호출할 콜백 (attempt, exception)
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        logger.error(
                            "%s 최종 실패 (%d/%d): %s",
                            func.__name__, attempt, max_attempts, e,
                        )
                        raise

                    logger.warning(
                        "%s 재시도 (%d/%d): %s (%.1f초 후)",
                        func.__name__, attempt, max_attempts, e, current_delay,
                    )

                    if on_retry:
                        try:
                            result = on_retry(attempt, e)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            pass

                    await asyncio.sleep(current_delay)
                    current_delay *= backoff

        return wrapper

    return decorator


class RecoveryRecord:
    """Chrome 복구 기록."""

    def __init__(self, success: bool, reason: str, timestamp: datetime):
        self.success = success
        self.reason = reason
        self.timestamp = timestamp


class ChromeHealthChecker:
    """Chrome 상태 모니터링 및 자동 복구."""

    def __init__(self):
        self._consecutive_failures = 0
        self._max_failures = 3  # 연속 실패 임계값
        self._last_recovery: datetime | None = None
        self._recovery_cooldown = timedelta(minutes=5)
        self._recovery_history: list[RecoveryRecord] = []
        self._max_history = 20
        self._total_recoveries = 0
        self._total_recovery_failures = 0
        # 알림 콜백 (bot에서 설정)
        self._notify_callback = None

    def set_notify_callback(self, callback) -> None:
        """디스코드 알림 콜백 설정. callback(message: str) -> coroutine"""
        self._notify_callback = callback

    async def _notify(self, message: str) -> None:
        """디스코드 로그 채널에 알림 전송."""
        if self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception:
                pass

    async def check_and_recover(self) -> bool:
        """Chrome 상태 확인 및 필요시 복구.

        Returns:
            True: 정상 또는 복구 성공, False: 복구 실패
        """
        from src.crawlers.chrome_cdp import cdp_manager

        # 포트 열려 있는지 확인
        if await cdp_manager._is_debug_port_open():
            self._consecutive_failures = 0
            return True

        # Chrome이 죽었음
        self._consecutive_failures += 1
        logger.warning(
            "Chrome 응답 없음 (연속 실패: %d/%d)",
            self._consecutive_failures, self._max_failures,
        )

        if self._consecutive_failures < self._max_failures:
            await self._notify(
                f"⚠️ Chrome 응답 없음 (연속 {self._consecutive_failures}/{self._max_failures}회) — "
                f"{self._max_failures}회 도달 시 자동 복구 시도"
            )
            return False

        # 복구 쿨다운 확인
        now = datetime.now()
        if self._last_recovery and (now - self._last_recovery) < self._recovery_cooldown:
            remaining = self._recovery_cooldown - (now - self._last_recovery)
            await self._notify(
                f"⏳ Chrome 복구 쿨다운 중 (잔여: {remaining.seconds}초) — "
                f"마지막 복구: {self._last_recovery.strftime('%H:%M:%S')}"
            )
            return False

        # 자동 복구 시도
        logger.warning("Chrome 자동 복구 시작")
        await self._notify("🔧 Chrome 자동 복구 시작...")

        try:
            await cdp_manager.restart_chrome()
            self._consecutive_failures = 0
            self._last_recovery = now
            self._total_recoveries += 1
            self._add_history(RecoveryRecord(True, "자동 복구 성공", now))
            logger.info("Chrome 자동 복구 성공")
            await self._notify(
                f"✅ Chrome 자동 복구 성공 (총 {self._total_recoveries}회 복구)"
            )
            return True
        except Exception as e:
            self._total_recovery_failures += 1
            self._add_history(RecoveryRecord(False, str(e), now))
            logger.error("Chrome 자동 복구 실패: %s", e)
            await self._notify(
                f"❌ Chrome 자동 복구 실패: {e}\n"
                f"연속 실패: {self._consecutive_failures}회 | "
                f"총 복구 실패: {self._total_recovery_failures}회\n"
                f"수동 확인이 필요할 수 있습니다."
            )
            return False

    def report_success(self) -> None:
        """크롤링 성공 보고 — 실패 카운터 리셋."""
        self._consecutive_failures = 0

    def report_failure(self) -> None:
        """크롤링 실패 보고."""
        self._consecutive_failures += 1

    def _add_history(self, record: RecoveryRecord) -> None:
        """복구 이력 추가."""
        if len(self._recovery_history) >= self._max_history:
            self._recovery_history.pop(0)
        self._recovery_history.append(record)

    def get_health_summary(self) -> str:
        """Chrome 상태 요약 문자열."""
        lines = [
            f"연속 실패: {self._consecutive_failures}/{self._max_failures}",
            f"총 복구 성공: {self._total_recoveries}회",
            f"총 복구 실패: {self._total_recovery_failures}회",
        ]
        if self._last_recovery:
            lines.append(f"마지막 복구: {self._last_recovery.strftime('%Y-%m-%d %H:%M:%S')}")

        if self._recovery_history:
            lines.append("\n최근 복구 이력:")
            for r in self._recovery_history[-5:]:
                icon = "✅" if r.success else "❌"
                lines.append(f"  {icon} {r.timestamp.strftime('%m/%d %H:%M')} — {r.reason}")

        return "\n".join(lines)


# 싱글톤
chrome_health = ChromeHealthChecker()


class ErrorAggregator:
    """에러 집계기 — 로그 채널에 보내기 위한 에러 수집."""

    def __init__(self, max_errors: int = 50):
        self._errors: list[dict] = []
        self._max_errors = max_errors

    def add(self, source: str, error: Exception) -> None:
        if len(self._errors) >= self._max_errors:
            self._errors.pop(0)
        self._errors.append({
            "source": source,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "time": datetime.now(),
        })

    def get_recent(self, minutes: int = 30) -> list[dict]:
        cutoff = datetime.now() - timedelta(minutes=minutes)
        return [e for e in self._errors if e["time"] > cutoff]

    def get_summary(self, minutes: int = 30) -> str:
        recent = self.get_recent(minutes)
        if not recent:
            return "최근 에러 없음"

        lines = [f"최근 {minutes}분 에러: {len(recent)}건\n"]
        # 소스별 그룹핑
        by_source: dict[str, int] = {}
        for e in recent:
            by_source[e["source"]] = by_source.get(e["source"], 0) + 1

        for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
            lines.append(f"  {source}: {count}건")

        # 마지막 에러 상세
        last = recent[-1]
        lines.append(f"\n마지막 에러 ({last['source']}, {last['time'].strftime('%H:%M:%S')}):")
        lines.append(f"  {last['error']}")

        return "\n".join(lines)

    def clear(self) -> None:
        self._errors.clear()


error_aggregator = ErrorAggregator()
