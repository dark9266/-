# 소싱처 재가동 검증 하네스 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 소싱처 덤프가 "아직 실제로 되는가"를 **응답코드가 아니라 의미(데이터 유무)로** 판정하는 검증 하네스를 만든다.

**Architecture:** 순수 판정 함수(네트워크·DB 무관) 코어 + 기준선 조회(catalog_dump_items) + CLI 러너 3층. 판정 로직을 순수 함수로 분리해 네트워크 없이 전수 테스트하고, 러너는 offline(기본, 네트워크 0) / live(명시 opt-in) 2모드로 돈다.

**Tech Stack:** Python 3.12+, asyncio, aiosqlite, pytest(asyncio_mode=auto), stdlib json/dataclasses.

## Global Constraints

- Ruff: line-length 100, rules E/F/I/N/W. 한국어 = 주석·docstring·사용자 메시지 / 영어 = 코드 식별자.
- 모든 I/O async (aiosqlite). 단 순수 판정 함수는 동기 — 그래야 테스트가 싸다.
- **크림 API 호출 0건.** 이 하네스는 소싱처만 본다. 크림 관련 모듈 import 금지.
- 테스트는 `tests/conftest.py` 전역 안전망(tmp DB + 실송신 차단) 아래에서 돈다. 운영 DB 접근 금지.
- `sqlite3.Row` 는 `.get()` 불가 — `dict()` 변환 필수.
- live 모드는 **명시 플래그**로만. 기본은 offline.

## 배경 (왜 이걸 만드나)

`scripts/run_canary.py` 는 **크림 DB 매칭 정답셋**만 본다(product_id↔이름, 네트워크 0). 소싱처가
아직 긁히는지는 **아무도 안 본다**. `catalog_dump_items` 최근 활동이 2026-05-05 에서 멈춰 있어
현재 소싱처 상태는 사실이 아니라 **가설**이다(코덱스 A 판정, `docs/research/2026-07-28-source-technique-audit.md`).

판정 원칙 — **상태코드로 판정하지 않는다**:
- 200 이어도 상품 데이터 0 → **실패**
- 'enable JavaScript' 문구가 있어도 상품 데이터 있음 → **성공**
- 연속 페이지가 같은 상품 집합 반환 → 페이지네이션 고장
- 직전 덤프 대비 건수 급감 → 조용한 누락

## File Structure

- `src/ops/source_verify.py` — **신규**. 순수 판정 함수 5종 + `SourceVerdict` + 기준선 조회.
  판정만 담당하고 어댑터를 직접 호출하지 않는다(러너가 주입).
- `tests/fixtures/source_canaries.json` — **신규**. 소싱처별 기준 상품 모델번호 + 필수 필드 +
  최소 건수. `run_canary.py` 의 `canary_matches.json` 과 **별개 파일**(관심사가 다르다).
- `scripts/verify_sources.py` — **신규**. CLI. offline(기본) / live(`--live`) 2모드.
- `tests/test_source_verify.py` — **신규**. 판정 함수 전수 + 기준선 조회 + fixture 정합성.

---

### Task 1: 판정 함수 코어

**Files:**
- Create: `src/ops/source_verify.py`
- Test: `tests/test_source_verify.py`

**Interfaces:**
- Consumes: 없음(순수 함수).
- Produces: `SourceVerdict(source: str, ok: bool, reasons: list[str], metrics: dict)`,
  `judge_has_data(items: list[dict]) -> tuple[bool, str]`,
  `judge_canary_present(items: list[dict], expected: list[str], key: str = "model_no") -> tuple[bool, str]`,
  `judge_field_fill(items: list[dict], fields: list[str], min_rate: float) -> tuple[bool, str, dict]`,
  `judge_pagination(pages: list[list[dict]], key: str = "model_no") -> tuple[bool, str]`,
  `judge_volume(count: int, baseline: int, max_drop: float) -> tuple[bool, str]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_source_verify.py
from src.ops.source_verify import (
    judge_canary_present,
    judge_field_fill,
    judge_has_data,
    judge_pagination,
    judge_volume,
)


def test_has_data_rejects_empty_even_when_fetch_succeeded():
    """200 이어도 상품 0 이면 실패 — 상태코드로 판정하지 않는다."""
    ok, reason = judge_has_data([])
    assert not ok
    assert "0건" in reason


def test_has_data_accepts_items_even_with_challenge_text():
    """challenge 문구가 섞여 있어도 데이터가 있으면 통과 — 오판 방지."""
    items = [{"model_no": "FV1920-001", "_raw": "please enable JavaScript"}]
    ok, _ = judge_has_data(items)
    assert ok


def test_canary_present_detects_missing_reference_product():
    items = [{"model_no": "AAA-111"}]
    ok, reason = judge_canary_present(items, ["FV1920-001"])
    assert not ok
    assert "FV1920-001" in reason


def test_canary_present_is_case_and_space_insensitive():
    items = [{"model_no": " fv1920-001 "}]
    ok, _ = judge_canary_present(items, ["FV1920-001"])
    assert ok


def test_field_fill_flags_low_rate():
    items = [{"model_no": "A", "price": 1000}, {"model_no": "B", "price": None}]
    ok, reason, rates = judge_field_fill(items, ["price"], min_rate=0.9)
    assert not ok
    assert rates["price"] == 0.5
    assert "price" in reason


def test_pagination_detects_identical_consecutive_pages():
    page = [{"model_no": "A"}, {"model_no": "B"}]
    ok, reason = judge_pagination([page, list(page)])
    assert not ok
    assert "동일" in reason


def test_pagination_accepts_progressing_pages():
    ok, _ = judge_pagination([[{"model_no": "A"}], [{"model_no": "B"}]])
    assert ok


def test_volume_flags_sharp_drop():
    ok, reason = judge_volume(count=100, baseline=1000, max_drop=0.5)
    assert not ok
    assert "급감" in reason


def test_volume_passes_when_no_baseline():
    """기준선이 없으면(첫 실행) 급감 판정을 하지 않는다 — 거짓 경보 방지."""
    ok, _ = judge_volume(count=10, baseline=0, max_drop=0.5)
    assert ok
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ops.source_verify'`

- [ ] **Step 3: 최소 구현을 쓴다**

```python
# src/ops/source_verify.py
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

from dataclasses import dataclass, field


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
    """필수 필드가 의미 있게 채워지는가(파서가 껍데기만 긁는 경우 탐지)."""
    rates: dict[str, float] = {}
    if not items:
        return False, "샘플 0건 — 충실도 판정 불가", rates
    for f in fields:
        filled = sum(1 for i in items if i.get(f) not in (None, "", []))
        rates[f] = round(filled / len(items), 3)
    low = [f for f, r in rates.items() if r < min_rate]
    if low:
        return False, f"필드 충실도 미달(<{min_rate}): {', '.join(low)}", rates
    return True, "", rates


def judge_pagination(pages: list[list[dict]], key: str = "model_no") -> tuple[bool, str]:
    """연속 페이지가 같은 집합을 돌려주면 페이지네이션이 고장난 것이다."""
    for idx in range(1, len(pages)):
        prev = {_norm(i.get(key)) for i in pages[idx - 1]}
        cur = {_norm(i.get(key)) for i in pages[idx]}
        if prev and prev == cur:
            return False, f"페이지 {idx}·{idx + 1} 이 동일 집합 — 페이지네이션 정지"
    return True, ""


def judge_volume(count: int, baseline: int, max_drop: float) -> tuple[bool, str]:
    """직전 덤프 대비 급감 탐지. 기준선이 없으면 판정하지 않는다(거짓 경보 방지)."""
    if baseline <= 0:
        return True, ""
    drop = 1 - (count / baseline)
    if drop > max_drop:
        return False, f"건수 급감: {baseline} → {count} ({drop:.0%} 감소)"
    return True, ""
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q`
Expected: `9 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ops/source_verify.py tests/test_source_verify.py
git commit -m "feat(verify): 소싱처 의미 기반 판정 함수 5종 — 상태코드가 아니라 데이터로 판정"
```

---

### Task 2: canary fixture + 정합성 검증

**Files:**
- Create: `tests/fixtures/source_canaries.json`
- Modify: `src/ops/source_verify.py` (로더 추가)
- Test: `tests/test_source_verify.py` (추가)

**Interfaces:**
- Consumes: Task 1 의 `SourceVerdict`.
- Produces: `load_canaries(path: Path | None = None) -> dict[str, dict]` —
  소싱처명 → `{"canary_model_numbers": list[str], "required_fields": list[str],
  "min_fill_rate": float, "max_drop": float}`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_source_verify.py 에 추가
from src.ops.source_verify import load_canaries


def test_canary_fixture_loads_and_has_required_keys():
    specs = load_canaries()
    assert specs, "fixture 가 비어 있다"
    for source, spec in specs.items():
        assert isinstance(spec["canary_model_numbers"], list), source
        assert isinstance(spec["required_fields"], list), source
        assert 0 < spec["min_fill_rate"] <= 1.0, source
        assert 0 < spec["max_drop"] < 1.0, source


def test_canary_fixture_sources_exist_in_runtime_registry():
    """fixture 에 적힌 소싱처는 실제 레지스트리에 있어야 한다 — 오타 방지."""
    from src.core.runtime import _ADAPTER_REGISTRY

    registered = {name for name, _cls in _ADAPTER_REGISTRY}
    unknown = set(load_canaries()) - registered
    assert not unknown, f"레지스트리에 없는 소싱처: {unknown}"
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q -k canary_fixture`
Expected: FAIL — `ImportError: cannot import name 'load_canaries'`

- [ ] **Step 3: fixture 와 로더를 쓴다**

```json
{
  "_meta": {
    "purpose": "소싱처가 아직 실제로 긁히는지 판정하는 기준. run_canary.py 의 크림 매칭 정답셋과 별개다.",
    "created_at": "2026-07-28",
    "policy": "canary_model_numbers 는 해당 소싱처에서 상시 판매되는 상품의 모델번호. 단종되면 교체한다.",
    "note": "min_fill_rate/max_drop 은 초기 보수값. 1회차 실측 후 보정한다."
  },
  "musinsa": {
    "canary_model_numbers": ["DC0774-101", "553558-040"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "29cm": {
    "canary_model_numbers": ["FV1920-001", "HQ7540-100"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "nike": {
    "canary_model_numbers": ["AV3595-013", "CD6404-105"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "kasina": {
    "canary_model_numbers": ["IH9256-010", "HQ7540-002"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "abcmart": {
    "canary_model_numbers": ["U574WR2", "U574LGMG"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "tune": {
    "canary_model_numbers": ["HF0074-001"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  },
  "wconcept": {
    "canary_model_numbers": ["FZ2068-100", "HQ7540-002"],
    "required_fields": ["model_no", "name", "url"],
    "min_fill_rate": 0.9,
    "max_drop": 0.5
  }
}
```

```python
# src/ops/source_verify.py 에 추가
import json
from pathlib import Path

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "source_canaries.json"
)


def load_canaries(path: Path | None = None) -> dict[str, dict]:
    """소싱처별 검증 기준 로드. `_meta` 키는 설명용이라 제외한다."""
    target = path or _FIXTURE
    with target.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q`
Expected: `11 passed`

- [ ] **Step 5: 커밋**

```bash
git add tests/fixtures/source_canaries.json src/ops/source_verify.py tests/test_source_verify.py
git commit -m "feat(verify): 소싱처 canary fixture 7곳 + 레지스트리 정합성 테스트"
```

---

### Task 3: 기준선 조회 + 종합 판정

**Files:**
- Modify: `src/ops/source_verify.py`
- Test: `tests/test_source_verify.py` (추가)

**Interfaces:**
- Consumes: Task 1 판정 함수, Task 2 `load_canaries`.
- Produces: `async def baseline_counts(db_path: str) -> dict[str, int]` —
  `catalog_dump_items` 의 소싱처별 행 수.
  `def evaluate(source: str, items: list[dict], spec: dict, baseline: int,
  pages: list[list[dict]] | None = None) -> SourceVerdict`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_source_verify.py 에 추가
import aiosqlite
import pytest

from src.ops.source_verify import baseline_counts, evaluate


@pytest.fixture
async def dump_db(tmp_path):
    path = str(tmp_path / "t.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE catalog_dump_items (source TEXT, model_no TEXT, name TEXT, url TEXT)"
        )
        await db.executemany(
            "INSERT INTO catalog_dump_items VALUES (?, ?, ?, ?)",
            [("29cm", "A", "n", "u"), ("29cm", "B", "n", "u"), ("nike", "C", "n", "u")],
        )
        await db.commit()
    return path


async def test_baseline_counts_reads_per_source_rows(dump_db):
    counts = await baseline_counts(dump_db)
    assert counts["29cm"] == 2
    assert counts["nike"] == 1


async def test_baseline_counts_returns_empty_when_table_missing(tmp_path):
    """테이블이 없는 새 DB 여도 죽지 않는다 — 첫 실행 방어."""
    assert await baseline_counts(str(tmp_path / "empty.db")) == {}


def test_evaluate_passes_healthy_source():
    spec = {
        "canary_model_numbers": ["A"],
        "required_fields": ["model_no", "name"],
        "min_fill_rate": 0.9,
        "max_drop": 0.5,
    }
    items = [{"model_no": "A", "name": "x"}, {"model_no": "B", "name": "y"}]
    v = evaluate("29cm", items, spec, baseline=2)
    assert v.ok
    assert v.reasons == []
    assert v.metrics["count"] == 2


def test_evaluate_collects_every_failure_reason():
    """실패 사유를 첫 건에서 끊지 않고 전부 모은다 — 한 번에 고치게."""
    spec = {
        "canary_model_numbers": ["MISSING"],
        "required_fields": ["price"],
        "min_fill_rate": 0.9,
        "max_drop": 0.5,
    }
    items = [{"model_no": "A", "price": None}]
    v = evaluate("29cm", items, spec, baseline=1000)
    assert not v.ok
    assert len(v.reasons) >= 3  # canary 미검출 + 필드 충실도 + 급감
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q -k "baseline or evaluate"`
Expected: FAIL — `ImportError: cannot import name 'baseline_counts'`

- [ ] **Step 3: 구현을 쓴다**

```python
# src/ops/source_verify.py 에 추가
import aiosqlite


async def baseline_counts(db_path: str) -> dict[str, int]:
    """직전 덤프 규모 — `catalog_dump_items` 의 소싱처별 행 수.

    테이블이 없는 새 DB 에서도 죽지 않는다(첫 실행 방어). 기준선이 없으면
    급감 판정을 건너뛰므로 거짓 경보가 나지 않는다.
    """
    try:
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
    except aiosqlite.Error:
        return {}


def evaluate(
    source: str,
    items: list[dict],
    spec: dict,
    baseline: int,
    pages: list[list[dict]] | None = None,
) -> SourceVerdict:
    """한 소싱처의 덤프 결과를 종합 판정.

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
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q`
Expected: `15 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ops/source_verify.py tests/test_source_verify.py
git commit -m "feat(verify): 기준선 조회 + 종합 판정 — 실패 사유 전수 수집"
```

---

### Task 4: CLI 러너 (offline 기본 / live opt-in)

**Files:**
- Create: `scripts/verify_sources.py`
- Test: `tests/test_source_verify.py` (추가)

**Interfaces:**
- Consumes: Task 1~3 전부.
- Produces: CLI. `python3 scripts/verify_sources.py [--live] [--source NAME]`.
  종료코드 0=전원 통과, 1=1곳 이상 실패.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_source_verify.py 에 추가
def test_cli_module_exposes_offline_report():
    """offline 모드는 네트워크 없이 기준선·fixture 정합성만 보고한다."""
    import importlib.util
    from pathlib import Path

    spec_path = Path(__file__).resolve().parents[1] / "scripts" / "verify_sources.py"
    spec = importlib.util.spec_from_file_location("verify_sources", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "build_offline_report")
    assert hasattr(mod, "main")


async def test_offline_report_lists_sources_without_baseline(tmp_path):
    import importlib.util
    from pathlib import Path

    spec_path = Path(__file__).resolve().parents[1] / "scripts" / "verify_sources.py"
    spec = importlib.util.spec_from_file_location("verify_sources", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    report = await mod.build_offline_report(str(tmp_path / "none.db"))
    assert report["sources"], "fixture 소싱처가 보고에 없다"
    assert all(r["baseline"] == 0 for r in report["sources"])
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q -k cli or offline_report`
Expected: FAIL — `FileNotFoundError: scripts/verify_sources.py`

- [ ] **Step 3: CLI 를 쓴다**

```python
#!/usr/bin/env python3
"""소싱처 재가동 검증 러너.

    python3 scripts/verify_sources.py              # offline — 네트워크 0
    python3 scripts/verify_sources.py --live       # 실제 덤프 1회씩 (소싱처 GET)
    python3 scripts/verify_sources.py --live --source 29cm

**크림 API 는 건드리지 않는다** — 이 하네스는 소싱처만 본다.
offline 은 기준선·fixture 정합성만 확인하고, live 는 어댑터 `dump_catalog()` 를
1회 돌려 의미 기반 판정을 적용한다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.ops.source_verify import (  # noqa: E402
    baseline_counts,
    evaluate,
    load_canaries,
)


async def build_offline_report(db_path: str) -> dict:
    """네트워크 0 — 기준선과 fixture 만 대조한다."""
    specs = load_canaries()
    base = await baseline_counts(db_path)
    rows = [
        {
            "source": name,
            "baseline": base.get(name, 0),
            "canaries": len(spec.get("canary_model_numbers", [])),
        }
        for name, spec in sorted(specs.items())
    ]
    return {"mode": "offline", "sources": rows}


async def run_live(db_path: str, only: str | None) -> int:
    """어댑터 `dump_catalog()` 1회씩 실행 후 판정. 실패 수를 반환한다."""
    from src.core.event_bus import EventBus
    from src.core.runtime import _ADAPTER_REGISTRY

    specs = load_canaries()
    base = await baseline_counts(db_path)
    bus = EventBus()
    failures = 0
    for name, cls in _ADAPTER_REGISTRY:
        if name not in specs or (only and name != only):
            continue
        try:
            adapter = cls(bus, db_path)
            _event, items = await adapter.dump_catalog()
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 판정 대상이다
            print(f"  ✗ {name}: 덤프 예외 — {exc}")
            failures += 1
            continue
        verdict = evaluate(name, items, specs[name], base.get(name, 0))
        mark = "✓" if verdict.ok else "✗"
        print(f"  {mark} {name}: {verdict.metrics['count']}건 "
              f"(기준선 {verdict.metrics['baseline']})")
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
            print(f"  · {row['source']:<14} 기준선 {row['baseline']:>6,} "
                  f"| canary {row['canaries']}건")
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
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `python3 -m pytest tests/test_source_verify.py -q && python3 scripts/verify_sources.py`
Expected: `17 passed`, 그리고 CLI 가 소싱처별 기준선 표를 출력하고 종료코드 0

- [ ] **Step 5: 커밋**

```bash
git add scripts/verify_sources.py tests/test_source_verify.py
git commit -m "feat(verify): 소싱처 검증 CLI — offline 기본 / live opt-in"
```

---

## Self-Review

**1. Spec coverage** — 코덱스가 요구한 5개 판정 항목 전부 대응:
canary 검출(Task 1·2) · 페이지네이션 진행(Task 1) · 필드 충실도(Task 1) · 건수 급감(Task 1·3) ·
상태코드 아닌 데이터 기반 성공 판정(`judge_has_data`, Task 1).

**2. Placeholder scan** — TBD·"적절히 처리" 없음. 모든 코드 스텝에 실제 코드 포함.

**3. Type consistency** — `SourceVerdict(source, ok, reasons, metrics)` 가 Task 1 정의 →
Task 3 `evaluate` 반환 → Task 4 소비까지 동일. `load_canaries`/`baseline_counts`/`evaluate`
이름이 Task 2→3→4 에서 일관.

**미해결(의도적 스코프 밖)**: live 모드에서 `pages` 인자를 채우려면 어댑터가 페이지 단위 산출을
노출해야 한다 — 현재 `dump_catalog()` 는 평탄한 리스트만 준다. 페이지네이션 판정은 함수·테스트로
준비만 해두고, 어댑터 인터페이스 확장은 **후속 조각**으로 분리한다(이 계획서 범위 밖).

---

## ⚠️ Task 4 설계 변경 (2026-07-28, 코덱스 적대검증 반영)

Task 1~3 구현 후 코덱스 B등급 적대검증(`reports/codex/codex_6924ec77c01e71b866e1.md`)에서
**REAL 5건**이 나왔다(수용 5 / 거부 0). Task 1~3 은 봉합 완료했고, **아래 2건은 Task 4 설계
자체를 바꾼다** — 원래 계획서대로 Task 4 를 만들면 전 소싱처가 거짓 실패한다.

### 변경 1 — `evaluate(items=...)` 에 덤프 원본을 그대로 넣으면 안 된다

원래 Task 4 는 `adapter.dump_catalog()` 결과를 `evaluate()` 에 바로 넣었다. **틀렸다.**
`dump_catalog()` 는 **소싱처 원본 스키마**를 그대로 돌려준다 — 어댑터마다 키가 다르다
(`model_number`·`productCode`·`productManagementCd`·`sku`·`STYLE_INFO`…). AST 로 확인한 사실:

```
twentynine_cm  dump_catalog  model_no 등장: False  |  match_to_kream  True
salomon        dump_catalog  model_no 등장: False  |  match_to_kream  True
musinsa        dump_catalog  model_no 등장: False  |  match_to_kream  True
```

즉 `model_no` 는 **매칭 단계에서 상품명 파싱으로 비로소 생성**된다. 덤프 원본에는 없다.
그대로 넣으면 canary 미검출 + 필드 충실도 0% 로 **전 소싱처 거짓 실패**가 난다 —
계획서 서두에 적은 "거짓 실패는 사장이 하네스를 꺼버리게 만든다" 그 실패 모드다.

**~~Task 4 는 소싱처별 extractor 층을 먼저 둔다~~ — 이 안은 폐기했다.**

코덱스의 최소 수정안(소싱처별 extractor 로 표준 스키마 변환)을 검토하다 **더 나은 길**을
찾았다. 추출 방식이 소싱처마다 3갈래로 갈린다(실측):

| 소싱처 | model_no 유래 |
|---|---|
| kasina · tune · wconcept · 29cm | 키 직접(`productManagementCd` · `sku` · `model_number`) |
| musinsa · nike · 29cm(fallback) | `extract_model_from_name()` — **상품명 파싱** |
| abcmart | `_build_model(style, color)` — **조립 함수** |

별도 extractor 를 만들면 **어댑터 정규화 로직을 복제**하게 되고, 두 벌이 갈라지는 순간
하네스가 거짓 판정을 낸다(원래 잡으려던 병을 하네스가 새로 만드는 꼴).

**채택: 원장 델타(ledger delta) 방식.** 어댑터가 **자기 정규화로 이미 원장에 쓴다**:
`match_to_kream()` 안에서 `record_dump_item(source, model_no, name, url)` 호출 —
`model_no` 는 그 어댑터의 정규화를 그대로 거친 값이다.

Task 4 러너 절차:
1. `adapter.dump_catalog()` — 소싱처 네트워크 GET (**크림 호출 아님**)
2. `adapter.match_to_kream(items)` — **로컬 전용**. `kream_index` 는 sqlite 캐시라 HTTP 없음
   (실측 확인). 구독자 없는 `EventBus` 를 주입하면 `CandidateMatched` 는 fanout 대상이 없다.
3. `catalog_dump_items` 에서 `source=? AND last_seen_at >= run_start` 로 **이번 실행분만** 조회
   → 이 행들이 `{model_no, name, url}` 표준 스키마다. 그대로 `evaluate()` 에 넣는다.

**주의 — 두 수를 다 보고한다**: 원장 행은 어댑터 필터(품절·PB·모델번호 없음) 통과분이라
`dump_catalog()` 원본보다 적다. `raw_count`(덤프 원본)와 `ledger_count`(원장 델타)를 **둘 다**
metrics 에 넣어라. 급감 판정은 `ledger_count` 기준(파이프라인에 실제 도달한 수)이되,
`raw_count > 0` 인데 `ledger_count == 0` 이면 "덤프는 됐으나 전량 필터됨"을 **별도 사유**로
보고한다(덤프 실패와 구분되어야 한다).

### 변경 2 — 기준선은 `catalog_dump_items` 가 아니다

`catalog_dump_items` 는 `first_seen_at`·`last_seen_at`·`seen_count` 를 가진
**과거 발견 상품 누적 유니크 원장**이다(`src/core/dump_ledger.py`). 단종분이 쌓인 1,000건
원장과 정상 현재 덤프 499건을 비교하면 **거짓 급감**이 난다.

**Task 4 는 덤프 실행 스냅샷을 기준선으로 쓴다:**
- 테이블 `catalog_dump_runs(source TEXT, product_count INTEGER, finished_at REAL)` 신설 +
  덤프 성공 시 1행 기록. `CatalogDumped` 이벤트가 이미 `product_count` 를 갖고 있다.
- 조회는 이미 구현된 `last_dump_count(db_path, source) -> int | None` 을 쓴다.
  테이블이 없으면 `None` → `judge_volume` 이 급감 판정을 스킵한다(첫 실행 거짓 경보 방지).
- Task 1~3 의 `ledger_unique_counts()` 는 **급감 기준선으로 쓰지 마라** — 참고 지표 전용.

### Task 1~3 에서 이미 봉합한 것 (재작업 금지)
- 반올림 전 임계 비교(89.96% 가 90% 를 통과하던 경계 버그)
- DB 오류 전파(판정 불능이 통과로 둔갑하던 미탐 구멍) — 테이블 부재만 `{}`
- `SUPPORTED_SOURCES` 경량 registry + 집합 동등성 테스트(항목 삭제도 잡힌다) +
  `src.core.runtime` import 부작용 제거
- 키 없는 페이지를 "동일 집합" 이 아니라 **판정 불가** 로 처리

### 남은 THEORETICAL (Task 4 이후 판단)
- canary 에 `last_verified`/`expires_at` 이 없어 단종 SKU 하나가 영구 거짓 실패를 만들 수 있다.
  → 주기적 canary 갱신 절차 또는 만료 필드 도입 검토.
- fixture 커버리지 7/21. 나머지 14곳은 판정 자체가 없다 — "미지원"을 명시적 verdict 로
  드러낼지, canary 를 채울지 결정 필요.
