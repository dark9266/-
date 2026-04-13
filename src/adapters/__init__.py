"""소싱처 푸시 어댑터 패키지 (Phase 2.4+).

각 어댑터는 이벤트 프로듀서로서 동작:
    소싱처 카탈로그 덤프 → 크림 DB 모델번호 매칭 → event_bus 로 이벤트 발행

오케스트레이터 (src.core.orchestrator) 는 이벤트 컨슈머 레이어에서
나머지 체인(수익 계산·알림·체크포인트)을 담당한다. 어댑터는 오케스트레이터를
직접 참조하지 않는다 (의존성 역전).
"""

from src.adapters.kream_hot_watcher import KreamHotWatcher
from src.adapters.musinsa_adapter import MusinsaAdapter

__all__ = ["KreamHotWatcher", "MusinsaAdapter"]
