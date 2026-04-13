"""소싱처 푸시 어댑터 패키지 (Phase 2.4+).

각 어댑터는 이벤트 프로듀서로서 동작:
    소싱처 카탈로그 덤프 → 크림 DB 모델번호 매칭 → event_bus 로 이벤트 발행

오케스트레이터 (src.core.orchestrator) 는 이벤트 컨슈머 레이어에서
나머지 체인(수익 계산·알림·체크포인트)을 담당한다. 어댑터는 오케스트레이터를
직접 참조하지 않는다 (의존성 역전).
"""

from src.adapters.abcmart_adapter import AbcmartAdapter
from src.adapters.adidas_adapter import AdidasAdapter
from src.adapters.arcteryx_adapter import ArcteryxAdapter
from src.adapters.asics_adapter import AsicsAdapter
from src.adapters.beaker_adapter import BeakerAdapter
from src.adapters.eql_adapter import EqlAdapter
from src.adapters.hoka_adapter import HokaAdapter
from src.adapters.kasina_adapter import KasinaAdapter
from src.adapters.kream_delta_watcher import KreamDeltaWatcher
from src.adapters.kream_hot_watcher import KreamHotWatcher
from src.adapters.musinsa_adapter import MusinsaAdapter
from src.adapters.nbkorea_adapter import NbkoreaAdapter
from src.adapters.nike_adapter import NikeAdapter
from src.adapters.patagonia_adapter import PatagoniaAdapter
from src.adapters.puma_adapter import PumaAdapter
from src.adapters.salomon_adapter import SalomonAdapter
from src.adapters.thehandsome_adapter import ThehandsomeAdapter
from src.adapters.thenorthface_adapter import TheNorthFaceAdapter
from src.adapters.tune_adapter import TuneAdapter
from src.adapters.twentynine_cm_adapter import TwentynineCmAdapter
from src.adapters.vans_adapter import VansAdapter
from src.adapters.wconcept_adapter import WconceptAdapter
from src.adapters.worksout_adapter import WorksoutAdapter

__all__ = [
    "AbcmartAdapter",
    "AdidasAdapter",
    "ArcteryxAdapter",
    "AsicsAdapter",
    "BeakerAdapter",
    "EqlAdapter",
    "HokaAdapter",
    "KasinaAdapter",
    "KreamDeltaWatcher",
    "KreamHotWatcher",
    "MusinsaAdapter",
    "NbkoreaAdapter",
    "NikeAdapter",
    "PatagoniaAdapter",
    "PumaAdapter",
    "SalomonAdapter",
    "ThehandsomeAdapter",
    "TheNorthFaceAdapter",
    "TuneAdapter",
    "TwentynineCmAdapter",
    "VansAdapter",
    "WconceptAdapter",
    "WorksoutAdapter",
]
