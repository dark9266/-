"""에러 처리 유틸리티.

크롤링 재시도, 글로벌 에러 핸들링.
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
