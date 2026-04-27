"""이벤트 버스 — asyncio 기반 멀티토픽 pub/sub (Phase 2.2).

v3 푸시 전환 런타임 코어의 중앙 신경계.
소싱처 덤퍼 → 매칭 엔진 → 수익 검증 → 알림 → 피드백 루프까지
각 단계가 이벤트 버스로만 소통하도록 설계한다.

이벤트 타입 5종:
    - CatalogDumped    : 소싱처 카탈로그 덤프 완료
    - CandidateMatched : 크림 DB 매칭 후보 발견
    - ProfitFound      : 수익성 임계 통과
    - AlertSent        : Discord 알림 발송 (Phase 4 피드백 루프 대비 슬롯)
    - AlertFollowup    : 알림 사후 추적 (Phase 4 슬롯)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass


class Event:
    """모든 이벤트의 베이스 마커 클래스.

    `EventBus.publish` 의 타입 검증에 사용된다.
    """


@dataclass(frozen=True)
class CatalogDumped(Event):
    """소싱처 카탈로그 전수 덤프 완료."""

    source: str
    product_count: int
    dumped_at: float


@dataclass(frozen=True)
class CandidateMatched(Event):
    """소싱처 상품이 크림 DB 모델번호와 매칭된 후보.

    `available_sizes` 는 소싱처에서 실제 재고 있는 사이즈 목록 (옵션).
    runtime 핸들러가 크림 size_prices 와 교집합을 계산해 실재 판매 가능한
    사이즈만 수익 계산 대상에 포함시킨다. 비어 있으면 교집합 가드 미적용
    (기존 listing-only 어댑터 하위호환).

    `color_name` 은 사이트→크림 색상 disambiguation 결과 (한글). 색상별
    fan-out 어댑터에서만 채움. 미구현 어댑터는 ""(미표시).
    """

    source: str
    kream_product_id: int
    model_no: str
    retail_price: int
    size: str
    url: str
    available_sizes: tuple = ()
    color_name: str = ""


@dataclass(frozen=True)
class ProfitFound(Event):
    """수익성 하드 플로어를 통과한 기회.

    `size_profits` 는 사이즈별 {size, kream_sell_price, net_profit, roi} tuple.
    kream_delta_client 이 size_prices 전체를 보존해 알림에서 사이즈별 수익표를
    그릴 수 있게 한다. 비어 있으면 publisher 는 단일 요약만 렌더.
    """

    source: str
    kream_product_id: int
    model_no: str
    size: str
    retail_price: int
    kream_sell_price: int
    net_profit: int
    roi: float
    signal: str
    volume_7d: int
    url: str
    size_profits: tuple = ()
    color_name: str = ""
    # Phase 1.5 — Chrome 확장 catch 적용 흔적
    catch_applied: bool = False
    original_retail: int | None = None


@dataclass(frozen=True)
class AlertSent(Event):
    """Discord 알림 발송 완료. Phase 4 피드백 루프 슬롯."""

    alert_id: int
    kream_product_id: int
    signal: str
    fired_at: float


@dataclass(frozen=True)
class AlertFollowup(Event):
    """알림 사후 추적 체크포인트. Phase 4 피드백 루프 슬롯."""

    alert_id: int
    checked_at: float


class EventBus:
    """asyncio 기반 멀티토픽 pub/sub 버스.

    - 타입별로 구독자 큐 목록을 관리한다
    - `publish` 는 해당 타입 구독자 모든 큐로 fanout
    - 구독자 없는 타입 publish 는 noop (통계는 기록)
    - Event 서브클래스 인스턴스만 publish 허용
    """

    def __init__(self) -> None:
        self._subscribers: dict[type[Event], list[asyncio.Queue[Event]]] = defaultdict(list)
        self._published: dict[type[Event], int] = defaultdict(int)

    def subscribe(
        self, event_type: type[Event], *, maxsize: int = 0
    ) -> asyncio.Queue[Event]:
        """해당 타입 전용 신규 큐를 생성·등록해 돌려준다."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers[event_type].append(queue)
        return queue

    def unsubscribe(self, event_type: type[Event], queue: asyncio.Queue[Event]) -> None:
        """구독자 큐를 목록에서 제거. 없으면 noop."""
        subs = self._subscribers.get(event_type)
        if not subs:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass

    async def publish(self, event: Event) -> None:
        """이벤트를 해당 타입 구독자 모든 큐에 fanout."""
        if not isinstance(event, Event):
            raise TypeError(
                f"publish requires Event instance, got {type(event).__name__}"
            )
        event_type = type(event)
        self._published[event_type] += 1
        subs = self._subscribers.get(event_type)
        _src = getattr(event, "source", "?")
        import logging
        _log = logging.getLogger(__name__)
        if not subs:
            _log.warning(
                "[diag-bus-publish] NO-SUBSCRIBERS src=%s type=%s evt_type_id=%s",
                _src, event_type.__name__, id(event_type),
            )
            return
        for queue in list(subs):
            await queue.put(event)
            _log.info(
                "[diag-bus-publish] src=%s type=%s evt_type_id=%s "
                "target_q_id=%s qsize_after_put=%d",
                _src, event_type.__name__, id(event_type),
                id(queue), queue.qsize(),
            )

    def published_count(self, event_type: type[Event]) -> int:
        """타입별 누적 발행 수 (관측/디버깅용)."""
        return self._published.get(event_type, 0)

    def subscriber_count(self, event_type: type[Event]) -> int:
        """타입별 현재 구독자 수."""
        return len(self._subscribers.get(event_type, []))


__all__ = [
    "AlertFollowup",
    "AlertSent",
    "CandidateMatched",
    "CatalogDumped",
    "Event",
    "EventBus",
    "ProfitFound",
]
