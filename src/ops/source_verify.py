"""소싱처 재가동 검증 — **의미 기반** 성공 판정.

## 왜
`scripts/run_canary.py` 는 크림 DB 매칭 정답셋만 본다. 소싱처가 아직 긁히는지는
아무도 안 봤다(`catalog_dump_items` 최근 활동 2026-05-05 정지). 그래서 현재
소싱처 수치는 "사실"이 아니라 "재가동 전 가설"이다.

## 원칙 — 상태코드로 판정하지 않는다
- 200 이어도 상품 0 → 실패
- challenge 문구가 있어도 상품 데이터 있음 → 성공
- 연속 페이지가 같은 집합 → 페이지네이션 고장
- 직전 덤프 대비 급감 → 조용한 누락

판정 함수는 전부 **순수 동기 함수**다 — 네트워크·DB 없이 테스트한다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class SourceVerdict:
    """한 소싱처의 검증 결과."""

    source: str
    ok: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def judge_has_data(items: list[dict]) -> tuple[bool, str]:
    """상품이 실제로 하나라도 있는가. 상태코드가 아니라 이것이 성공 기준이다."""
    if not items:
        return False, "상품 0건 — 응답은 왔으나 데이터가 없다(차단 또는 파서 불일치)"
    return True, ""


def judge_canary_present(
    items: list[dict], expected: list[str], key: str = "model_no"
) -> tuple[bool, str]:
    """기준 상품(canary)이 덤프에 잡히는가."""
    if not expected:
        return True, ""
    seen = {_norm(i.get(key)) for i in items}
    missing = [e for e in expected if _norm(e) not in seen]
    if missing:
        return False, f"기준 상품 미검출: {', '.join(missing)}"
    return True, ""


def judge_field_fill(
    items: list[dict], fields: list[str], min_rate: float
) -> tuple[bool, str, dict]:
    """필수 필드가 의미 있게 채워지는가(파서가 껍데기만 긁는 경우 탐지).

    비교는 **반올림 전 원시 비율**로 한다 — 0.8996 을 0.900 으로 반올림해
    임계와 비교하면 89.96% 가 90% 컷을 통과하는 경계 버그가 난다.
    `rates` 에 담기는 표시값만 반올림한다.
    """
    rates: dict[str, float] = {}
    if not items:
        return False, "샘플 0건 — 충실도 판정 불가", rates
    low: list[str] = []
    for f in fields:
        filled = sum(1 for i in items if i.get(f) not in (None, "", []))
        raw_rate = filled / len(items)
        rates[f] = round(raw_rate, 3)
        if raw_rate < min_rate:
            low.append(f)
    if low:
        return False, f"필드 충실도 미달(<{min_rate}): {', '.join(low)}", rates
    return True, "", rates


def judge_pagination(pages: list[list[dict]], key: str = "model_no") -> tuple[bool, str]:
    """연속 페이지가 같은 집합을 돌려주면 페이지네이션이 고장난 것이다.

    정규화 결과가 **전부 빈 문자열인 페이지**는 비교 대상에서 제외한다 —
    키가 아예 없는 두 페이지는 각각 `{""}` 가 되어 "동일 집합"으로 오판될
    수 있다. 그 경우 판정 불능을 실패 사유로 알린다(조용히 통과시키지 않는다).
    """
    for idx in range(1, len(pages)):
        prev = {_norm(i.get(key)) for i in pages[idx - 1]}
        cur = {_norm(i.get(key)) for i in pages[idx]}
        if prev == {""} or cur == {""}:
            return (
                False,
                f"키 `{key}` 를 가진 항목이 없어 페이지네이션 판정 불가"
                f"(페이지 {idx}·{idx + 1})",
            )
        if prev and prev == cur:
            return False, f"페이지 {idx}·{idx + 1} 이 동일 집합 — 페이지네이션 정지"
    return True, ""


def judge_volume(count: int, baseline: int | None, max_drop: float) -> tuple[bool, str]:
    """직전 덤프 대비 급감 탐지. 기준선이 없으면 판정하지 않는다(거짓 경보 방지)."""
    if baseline is None or baseline <= 0:
        return True, ""
    drop = 1 - (count / baseline)
    if drop > max_drop:
        return False, f"건수 급감: {baseline} → {count} ({drop:.0%} 감소)"
    return True, ""


_FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "source_canaries.json"
)

#: `tests/fixtures/source_canaries.json` 이 실제로 커버하는 소싱처 — 크림
#: watcher 까지 끌고 오는 `src.core.runtime` import 없이도 fixture 커버리지를
#: 집합 동등성으로 검증할 수 있게 경량 목록으로 고정한다. 항목 추가/삭제 시
#: fixture 와 함께 갱신할 것(테스트가 불일치를 잡는다).
SUPPORTED_SOURCES: frozenset[str] = frozenset(
    {"29cm", "abcmart", "kasina", "musinsa", "nike", "tune", "wconcept"}
)


def load_canaries(path: Path | None = None) -> dict[str, dict]:
    """소싱처별 검증 기준 로드. `_meta` 키는 설명용이라 제외한다."""
    target = path or _FIXTURE
    with target.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


async def ledger_unique_counts(db_path: str) -> dict[str, int]:
    """`catalog_dump_items` 의 소싱처별 **누적 유니크** 행 수.

    이건 **누적 유니크 원장**이지 직전 덤프 규모가 아니다 — `catalog_dump_items`
    는 과거에 한 번이라도 발견된 상품이 `first_seen_at/last_seen_at/seen_count`
    로 계속 누적되는 테이블이라(`src/core/dump_ledger.py`), 단종된 상품도 그대로
    남아 있다. 이 값을 급감 판정 기준선으로 쓰면 거짓 경보가 난다(예: 누적
    1,000건 원장 vs 정상 현재 덤프 499건을 비교하면 항상 "급감"으로 보인다).
    직전 덤프 규모가 필요하면 `last_dump_count()` 를 쓴다.

    테이블이 없는 새 DB 에서도 죽지 않는다(첫 실행 방어) — 이 경우만 `{}`.
    그 외 DB 오류(파일 손상 등)는 **전파**한다 — 판정 불능을 조용히 통과로
    둔갑시키지 않는다.
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_dump_items'"
        )
        if await cur.fetchone() is None:
            return {}
        cur = await db.execute(
            "SELECT source, COUNT(*) FROM catalog_dump_items GROUP BY source"
        )
        return {str(r[0]): int(r[1]) for r in await cur.fetchall()}


async def last_dump_count(db_path: str, source: str) -> int | None:
    """`catalog_dump_runs` 에서 해당 source 의 **가장 최근 덤프 실행** 건수.

    `catalog_dump_items` 누적 원장과 달리 이건 "직전 1회 덤프가 몇 건이었나"
    를 답한다 — 급감 판정 기준선으로는 이쪽이 맞다.

    테이블이 없으면 `None`(기준선 없음 → 급감 판정 스킵 신호). 이 함수는
    **조회만** 한다 — `catalog_dump_runs` 를 생성·기록하는 배선은 별도
    작업(Task 4) 범위다. 테이블 부재 외 DB 오류는 전파한다.
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_dump_runs'"
        )
        if await cur.fetchone() is None:
            return None
        cur = await db.execute(
            "SELECT product_count FROM catalog_dump_runs"
            " WHERE source = ? ORDER BY finished_at DESC LIMIT 1",
            (source,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row is not None else None


async def ensure_dump_runs_schema(db_path: str) -> None:
    """`catalog_dump_runs` 스냅샷 테이블 생성(멱등). 급감 판정 기준선 원장이다.

    `catalog_dump_items` 와 달리 이건 **실행 1회 단위 스냅샷**이라 단종 상품이
    누적되지 않는다 — `last_dump_count()` 가 여기서 "직전 1회 덤프 건수"를 읽는다.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS catalog_dump_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                product_count INTEGER NOT NULL,
                finished_at REAL NOT NULL
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dump_runs_source"
            " ON catalog_dump_runs(source, finished_at)"
        )
        await db.commit()


async def record_dump_run(db_path: str, source: str, product_count: int) -> None:
    """덤프 실행 1회 스냅샷을 `catalog_dump_runs` 에 기록.

    스키마가 없으면 여기서 함께 만든다(`ensure_dump_runs_schema` 멱등 재사용) —
    호출 순서를 강제하지 않아도 안전하다.
    """
    await ensure_dump_runs_schema(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO catalog_dump_runs (source, product_count, finished_at)"
            " VALUES (?, ?, ?)",
            (source, product_count, time.time()),
        )
        await db.commit()


async def ledger_delta(db_path: str, source: str, since: float) -> list[dict]:
    """`catalog_dump_items` 에서 `since` 이후 갱신된 행 — **이번 실행분만**.

    어댑터가 `match_to_kream()` 안에서 `record_dump_item()` 으로 이미 자기
    정규화(`model_no`)를 거쳐 원장에 쓴 값이다 — 별도 extractor 를 두지 않고
    이 원장을 그대로 표준 스키마(`model_no`/`name`/`url`)로 쓴다.

    테이블이 없으면 `[]`(첫 실행 방어). 그 외 DB 오류는 전파한다 —
    `ledger_unique_counts()` 와 동일 정책, 판정 불능을 통과로 둔갑시키지 않는다.
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_dump_items'"
        )
        if await cur.fetchone() is None:
            return []
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT model_no, name, url FROM catalog_dump_items"
            " WHERE source = ? AND last_seen_at >= ?",
            (source, since),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


def evaluate(
    source: str,
    items: list[dict],
    spec: dict,
    baseline: int | None,
    pages: list[list[dict]] | None = None,
) -> SourceVerdict:
    """한 소싱처의 덤프 결과를 종합 판정.

    `items` 는 **표준 스키마(model_no/name/url)로 정규화된 뒤** 들어와야
    한다 — 어댑터 `dump_catalog()` 원본은 소싱처마다 키가 달라(`model_number`·
    `productCode`·`sku` 등) 그대로 넣으면 전 소싱처 거짓 실패가 난다.

    실패 사유를 **첫 건에서 끊지 않고 전부 모은다** — 한 번 돌려 한 번에 고치게.
    """
    reasons: list[str] = []
    metrics: dict = {"count": len(items), "baseline": baseline}

    ok, why = judge_has_data(items)
    if not ok:
        reasons.append(why)
        return SourceVerdict(source, False, reasons, metrics)

    ok, why = judge_canary_present(items, spec.get("canary_model_numbers", []))
    if not ok:
        reasons.append(why)

    ok, why, rates = judge_field_fill(
        items, spec.get("required_fields", []), float(spec.get("min_fill_rate", 0.9))
    )
    metrics["fill_rates"] = rates
    if not ok:
        reasons.append(why)

    ok, why = judge_volume(len(items), baseline, float(spec.get("max_drop", 0.5)))
    if not ok:
        reasons.append(why)

    if pages:
        ok, why = judge_pagination(pages)
        if not ok:
            reasons.append(why)

    return SourceVerdict(source, not reasons, reasons, metrics)
