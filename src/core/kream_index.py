"""크림 모델번호 표준 인덱스 — 프로세스 싱글톤 + TTL 캐시.

배경:
    어댑터 19곳이 각자 `_load_kream_index()` 호출 → 한 사이클에만
    sqlite connection 수십개 생성 (47k 행 반복 로드). WAL 부풀음 +
    fd 누수 + 오버헤드 3종 세트의 주원인. 2026-04-18 incident 직후
    근본 대응의 Phase 3.

설계:
    - `get_kream_index(db_path)` → 프로세스 싱글톤 `KreamIndex` 반환.
    - `.get()` 은 TTL (기본 300s) 내 같은 dict 객체를 돌려준다.
    - 만료/초기 진입 시에만 `sync_connect` 1회 + 47k 행 스캔.
    - 캐시는 `dict[str, dict]` — 스트립 키 → kream row dict.
    - `kream_db_builder` 가 신규 상품을 넣은 직후라도 다음 TTL 경과
      시까지는 stale 수용 (덤프 사이클 5분 이상이라 사실상 무영향).

스트립 키 정의 (어댑터 19곳에서 공통):
    ``re.sub(r"[\\s\\-]", "", normalize_model_number(model_number))``

스코프 외 (각자 custom 인덱스 유지):
    - converse (dict[str, list[dict]] — 브랜드 필터 + 슬래시 split)
    - stussy (dict[str, list[dict]] — digit prefix 키)
    - patagonia (슬래시 split 기반 split_kream_model_numbers)
"""

from __future__ import annotations

import logging
import re
import threading
import time

from src.core.db import sync_connect
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SEC: float = 300.0
_STRIP_RE = re.compile(r"[\s\-]")


def strip_key(model_number: str) -> str:
    """어댑터 19곳에서 공통으로 쓰는 인덱스 키 정규화."""
    return _STRIP_RE.sub("", normalize_model_number(model_number))


class KreamIndex:
    """크림 DB `kream_products` 를 스트립 키로 인덱싱한 싱글톤 보관함."""

    def __init__(self, db_path: str, ttl_sec: float = _DEFAULT_TTL_SEC) -> None:
        self._db_path = db_path
        self._ttl = ttl_sec
        self._cache: dict[str, dict] | None = None
        self._loaded_at: float = 0.0
        self._load_count: int = 0
        self._lock = threading.Lock()

    def get(self) -> dict[str, dict]:
        """TTL 내 캐시 히트 시 즉시 반환, 아니면 재적재."""
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and (now - self._loaded_at) < self._ttl:
                return self._cache
            self._cache = self._load()
            self._loaded_at = now
            self._load_count += 1
            logger.info(
                "[kream_index] 재적재 #%d — rows=%d ttl=%ds",
                self._load_count, len(self._cache), int(self._ttl),
            )
            return self._cache

    def invalidate(self) -> None:
        """외부 트리거로 강제 만료 (e.g. kream_db_builder 직후)."""
        with self._lock:
            self._cache = None
            self._loaded_at = 0.0

    def _load(self) -> dict[str, dict]:
        with sync_connect(self._db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products WHERE model_number != ''"
            ).fetchall()
        index: dict[str, dict] = {}
        for row in rows:
            key = strip_key(row["model_number"])
            if key:
                index[key] = dict(row)
        return index


_instances: dict[str, KreamIndex] = {}
_instances_lock = threading.Lock()


def get_kream_index(
    db_path: str, ttl_sec: float = _DEFAULT_TTL_SEC
) -> KreamIndex:
    """프로세스 싱글톤 반환. `db_path` 별로 분리 (테스트 격리용)."""
    with _instances_lock:
        inst = _instances.get(db_path)
        if inst is None:
            inst = KreamIndex(db_path, ttl_sec=ttl_sec)
            _instances[db_path] = inst
        return inst


def reset_kream_index() -> None:
    """테스트/진단용 — 모든 싱글톤 인스턴스 폐기."""
    global _instances
    with _instances_lock:
        _instances = {}


__all__ = [
    "KreamIndex",
    "get_kream_index",
    "reset_kream_index",
    "strip_key",
]
