"""델타 엔진 — 크림 상품 스냅샷 해시 diff (Phase 3).

배경:
    hot watcher 가 130건 × 60초 전수 폴링 → 일 187,200회 크림 호출 발생.
    KREAM_DAILY_CAP=10,000 의 18.7배. 파일럿 즉시 차단.

해결:
    경량 조회(list/latest API 1~2회)로 현재 스냅샷을 얻은 뒤,
    이전 저장 해시와 비교해 "바뀐 상품만" 식별한다.
    변경된 것만 상세 sell_now 를 찍으므로 크림 호출량이 수십 배 감소.

핵심 설계 원칙:
    - 결정적 해시 (sha256) — 동일 입력 → 동일 출력
    - 해시 필드는 생성자 주입 (기본 sell_now_price/volume_7d/sold_count)
    - 삭제 감지 지원 (이전엔 있었는데 현재엔 없는 pid → stale)
    - aiosqlite 비동기, CREATE TABLE IF NOT EXISTS — 기존 스키마 건드리지 않음
    - mark_snapshot 은 detect_changes 호출 후 호출해야 다음 루프에서 '안 바뀜' 판정
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import aiosqlite

# ─── 기본 해시 대상 필드 ──────────────────────────────────
DEFAULT_HASH_FIELDS: tuple[str, ...] = (
    "sell_now_price",
    "volume_7d",
    "sold_count",
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kream_snapshot_hash (
    kream_product_id INTEGER PRIMARY KEY,
    hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kream_snapshot_updated
    ON kream_snapshot_hash(updated_at);
"""


def _extract_pid(product: dict[str, Any]) -> int | None:
    """dict 에서 product_id 를 정수로 추출. 변환 불가 시 None."""
    pid = product.get("kream_product_id")
    if pid is None:
        pid = product.get("product_id")
    if pid is None:
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def compute_hash(
    product: dict[str, Any],
    fields: tuple[str, ...] = DEFAULT_HASH_FIELDS,
) -> str:
    """지정 필드만 뽑아 정렬 JSON → sha256 hex digest.

    None 은 그대로 두고, 수치형은 문자열로 캐스팅해 결정적 표현을 만든다.
    dict 키 순서 독립 — `sort_keys=True`.
    """
    payload = {f: product.get(f) for f in fields}
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class DeltaEngine:
    """크림 상품 스냅샷 해시 기반 변경 감지기.

    생애주기:
        1. init() — 테이블 생성 (1회)
        2. detect_changes(current) — 변경/신규/삭제 추출
        3. (변경분만 외부 처리)
        4. mark_snapshot(current) — 해시 갱신 → 다음 루프 비교 기준
    """

    def __init__(
        self,
        db_path: str,
        *,
        hash_fields: tuple[str, ...] = DEFAULT_HASH_FIELDS,
    ) -> None:
        self._db_path = db_path
        self._hash_fields = hash_fields

    async def init(self) -> None:
        """kream_snapshot_hash 테이블 생성 (IF NOT EXISTS)."""
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            await conn.executescript(_SCHEMA_SQL)
            await conn.commit()

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    async def _load_existing(
        self, pids: list[int]
    ) -> dict[int, str]:
        """주어진 pid 집합의 기존 해시만 로드."""
        if not pids:
            return {}
        placeholders = ",".join("?" * len(pids))
        query = (
            f"SELECT kream_product_id, hash FROM kream_snapshot_hash "
            f"WHERE kream_product_id IN ({placeholders})"
        )
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(query, pids) as cur:
                rows = await cur.fetchall()
        return {int(r["kream_product_id"]): str(r["hash"]) for r in rows}

    async def _load_all_ids(self) -> set[int]:
        """저장된 모든 pid (삭제 감지용)."""
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            async with conn.execute(
                "SELECT kream_product_id FROM kream_snapshot_hash"
            ) as cur:
                rows = await cur.fetchall()
        return {int(r[0]) for r in rows}

    async def pending_count(self) -> int:
        """저장된 스냅샷 총 개수 — 관측/디버깅용."""
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM kream_snapshot_hash"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # 핵심 API
    # ------------------------------------------------------------------
    async def detect_changes(
        self, products: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """현재 스냅샷 리스트를 이전 해시와 비교.

        Returns
        -------
        (changed, stale_ids)
            changed: 해시가 바뀌었거나 신규인 products 의 원본 dict 리스트
            stale_ids: 이전엔 있었으나 현재 목록에 없는 pid (삭제/미노출)
        """
        # 현재 입력 → pid/해시 맵
        current_hashes: dict[int, str] = {}
        current_by_pid: dict[int, dict[str, Any]] = {}
        for p in products:
            pid = _extract_pid(p)
            if pid is None:
                continue
            current_hashes[pid] = compute_hash(p, self._hash_fields)
            current_by_pid[pid] = p

        pids = list(current_hashes.keys())
        existing = await self._load_existing(pids)

        changed: list[dict[str, Any]] = []
        for pid, h in current_hashes.items():
            prev = existing.get(pid)
            if prev is None or prev != h:
                changed.append(current_by_pid[pid])

        # 삭제 감지 — 이전에 있었는데 현재 입력에 없는 것
        all_stored = await self._load_all_ids()
        stale_ids = sorted(all_stored - set(pids))

        return changed, stale_ids

    async def mark_snapshot(self, products: list[dict[str, Any]]) -> None:
        """현재 스냅샷 해시를 저장/갱신.

        호출 주체는 detect_changes 처리가 끝난 뒤 이 함수로 마킹해야
        다음 루프에서 해당 상품이 '변경 없음' 으로 판정된다.
        """
        now = time.time()
        rows = []
        for p in products:
            pid = _extract_pid(p)
            if pid is None:
                continue
            rows.append((pid, compute_hash(p, self._hash_fields), now))
        if not rows:
            return
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            await conn.executemany(
                "INSERT INTO kream_snapshot_hash "
                "(kream_product_id, hash, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(kream_product_id) DO UPDATE SET "
                "hash=excluded.hash, updated_at=excluded.updated_at",
                rows,
            )
            await conn.commit()

    async def forget(self, pids: list[int]) -> None:
        """특정 pid 스냅샷 삭제 (stale 정리 등)."""
        if not pids:
            return
        placeholders = ",".join("?" * len(pids))
        async with aiosqlite.connect(self._db_path, timeout=30.0) as conn:
            await conn.execute(
                f"DELETE FROM kream_snapshot_hash "
                f"WHERE kream_product_id IN ({placeholders})",
                pids,
            )
            await conn.commit()


__all__ = [
    "DEFAULT_HASH_FIELDS",
    "DeltaEngine",
    "compute_hash",
]
