#!/usr/bin/env python3
"""소싱처 재가동 검증 러너 — 원장 델타(ledger delta) 방식.

    python3 scripts/verify_sources.py              # offline — 네트워크 0
    python3 scripts/verify_sources.py --live       # 실제 덤프 1회씩 (소싱처 GET)
    python3 scripts/verify_sources.py --live --source 29cm

**크림 API 는 건드리지 않는다** — 이 하네스는 소싱처만 본다.

## 왜 원장 델타인가 (2026-07-28, 코덱스 적대검증 반영 — 계획서 "Task 4 설계 변경" 참조)
`adapter.dump_catalog()` 는 소싱처 원본 스키마를 그대로 준다(`model_number`·`sku`·
`productManagementCd` 등 어댑터마다 다르다). 별도 extractor 를 두면 어댑터
정규화 로직을 복제하게 되고 갈라지는 순간 하네스가 거짓 판정을 낸다.
대신 어댑터가 `match_to_kream()` 안에서 `record_dump_item()` 으로 **자기
정규화 결과를 이미 원장(`catalog_dump_items`)에 쓴다** — 그 원장을 읽는다.

offline 모드는 fixture·DB 기준선만 대조(네트워크 0). live 모드는 어댑터
`dump_catalog()` → `match_to_kream()` 을 1회 돌린 뒤 원장 델타로 판정한다.
`match_to_kream()` 은 로컬 전용이다 — `kream_index` 는 sqlite 캐시(HTTP 없음),
구독자 없는 `EventBus` 는 `CandidateMatched` publish 시 fanout 대상이 없다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.ops.source_verify import (  # noqa: E402
    SourceVerdict,
    evaluate,
    last_dump_count,
    ledger_delta,
    ledger_unique_counts,
    load_canaries,
    record_dump_run,
)


async def build_offline_report(db_path: str) -> dict:
    """네트워크 0 — fixture 와 DB 기준선(누적 원장 + 직전 실행 스냅샷)만 대조한다."""
    specs = load_canaries()
    unique_counts = await ledger_unique_counts(db_path)
    rows = []
    for name, spec in sorted(specs.items()):
        rows.append(
            {
                "source": name,
                "ledger_unique": unique_counts.get(name, 0),
                "last_dump_count": await last_dump_count(db_path, name),
                "canaries": len(spec.get("canary_model_numbers", [])),
            }
        )
    return {"mode": "offline", "sources": rows}


async def verify_source_live(adapter, source: str, spec: dict, db_path: str) -> SourceVerdict:
    """어댑터 `dump_catalog()` → `match_to_kream()`(로컬 전용) → 원장 델타 판정.

    덤프·매칭 어느 쪽이 예외를 던지든 하네스가 죽지 않고 실패 사유로 잡는다
    (다른 소싱처 검증까지 막히면 안 된다).
    """
    run_start = time.time()
    try:
        _event, raw = await adapter.dump_catalog()
    except Exception as exc:  # noqa: BLE001 — 덤프 실패도 판정 대상이다
        return SourceVerdict(
            source, False, [f"덤프 예외: {exc}"], {"raw_count": 0, "ledger_count": 0}
        )

    try:
        await adapter.match_to_kream(raw)
    except Exception as exc:  # noqa: BLE001 — 매칭 실패도 판정 대상이다
        return SourceVerdict(
            source,
            False,
            [f"매칭 예외(덤프 {len(raw)}건은 됐으나 매칭이 깨짐): {exc}"],
            {"raw_count": len(raw), "ledger_count": 0},
        )

    items = await ledger_delta(db_path, source, run_start)
    baseline = await last_dump_count(db_path, source)
    verdict = evaluate(source, items, spec, baseline)

    raw_count = len(raw)
    ledger_count = len(items)
    verdict.metrics["raw_count"] = raw_count
    verdict.metrics["ledger_count"] = ledger_count

    if raw_count > 0 and ledger_count == 0:
        reason = (
            f"덤프 {raw_count}건은 됐으나 원장 0건 — 어댑터 필터"
            "(품절·PB·모델번호없음)에 전량 걸림(덤프 실패와 구분)"
        )
        verdict = replace(verdict, ok=False, reasons=[*verdict.reasons, reason])

    if verdict.ok:
        await record_dump_run(db_path, source, ledger_count)

    return verdict


async def run_live(db_path: str, only: str | None) -> int:
    """소싱처별 `verify_source_live` 실행. 실패 수를 반환한다."""
    from src.core.event_bus import EventBus
    from src.core.runtime import _ADAPTER_REGISTRY

    specs = load_canaries()
    if only is not None and only not in specs:
        # 2026-07-28 실행에서 spec 없는 소싱처가 루프 전체 skip → "실패 0곳"
        # 공허 통과로 위장됐다. 미지명은 판정 불가이므로 즉시 실패.
        available = ", ".join(sorted(specs))
        print(f"  ✗ {only}: canary spec 없음(미지 소싱처) — 판정 불가. 가용: {available}")
        return 1
    bus = EventBus()
    failures = 0
    for name, cls in _ADAPTER_REGISTRY:
        if name not in specs or (only and name != only):
            continue
        adapter = cls(bus, db_path)
        verdict = await verify_source_live(adapter, name, specs[name], db_path)
        mark = "✓" if verdict.ok else "✗"
        print(
            f"  {mark} {name}: raw {verdict.metrics.get('raw_count', 0)}건"
            f" ledger {verdict.metrics.get('ledger_count', 0)}건"
            f" (기준선 {verdict.metrics.get('baseline')})"
        )
        for reason in verdict.reasons:
            print(f"      - {reason}")
        if not verdict.ok:
            failures += 1
    return failures


async def _amain(args: argparse.Namespace) -> int:
    db_path = settings.db_path
    if not args.live:
        report = await build_offline_report(db_path)
        print("[verify-sources] OFFLINE (네트워크 0)")
        for row in report["sources"]:
            print(
                f"  · {row['source']:<14} 누적원장 {row['ledger_unique']:>6,}"
                f" | 직전덤프 {row['last_dump_count']}"
                f" | canary {row['canaries']}건"
            )
        print("  실 검증: --live 로 재실행")
        return 0

    print("[verify-sources] LIVE — 소싱처 덤프 1회씩 (크림 호출 0)")
    failures = await run_live(db_path, args.source)
    print(f"[verify-sources] 실패 {failures}곳")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="실제 덤프 실행 (기본: offline)")
    p.add_argument("--source", type=str, default=None, help="한 소싱처만 검증")
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
