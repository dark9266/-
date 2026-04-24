#!/usr/bin/env python3
"""diag_coverage.py — 5브랜드 매칭 누수 bucket 진단 하네스.

Tier 2 커버율 보강 plan `docs/superpowers/plans/...` 의 Phase 0.
크림 API 호출 0, 소싱처 POST 0, 순수 로컬 DB SELECT.

관측 지표 (브랜드별):
  1. 카탈로그 덤프 측 match_rate  (`catalog_dump_items` / `retail_products`)
  2. 크림 측 cover율               (kream_products.brand 중 retail 에 exact match)
  3. unmatched 샘플 20건 + bucket  (왜 매칭 실패했나 자동 라벨링)
  4. decision_log 최근 24h reason 분포

bucket 라벨링 규칙:
  A — 덤프된 model_no 가 크림에 없음 + 상품명 regex 도 추출 실패
      → 어댑터가 잘못된 필드(productId 등) 를 model_no 로 덤프하고 있을 가능성
  B — 덤프 model_no 있음 + extract_model_from_name(name) 결과도 크림에 없음
      → retail vs kream 정규화 포맷 차이 (하이픈/슬래시/자릿수)
  C — 덤프 model_no 가 크림 model_number 의 prefix(최소 6자) 로만 존재
      → 크림은 색상 suffix 를 포함하는데 retail 은 suffix 없이 덤프
  D — name 에 COLLAB/SUBTYPE 키워드 매칭
      → matching_guards 가 차단 (결과 확인: decision_log.reason='collab'/'subtype')
  E — 위 어디에도 안 걸림 (진짜 크림에 없는 상품이거나 기타)

CLI:
  python scripts/diag_coverage.py --brand all        # 5브랜드 전체 + Jordan
  python scripts/diag_coverage.py --brand nike
  python scripts/diag_coverage.py --brand nike --limit 40

산출물:
  data/diag/coverage_<ts>.jsonl — 샘플 1줄/row
  data/diag/coverage_<ts>.md    — 사람 읽기용 요약
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.core.dump_ledger import match_rate, unmatched_sample
from src.core.kream_index import get_kream_index, strip_key
from src.core.matching_guards import COLLAB_KEYWORDS, SUBTYPE_KEYWORDS
from src.matcher import extract_model_from_name

# 5 브랜드 + Jordan (nike_adapter Jordan 카테고리 누락 의심, 참고용)
SOURCES: list[str] = ["nike", "adidas", "nbkorea", "vans", "asics"]
SOURCES_WITH_EXTRA: list[str] = SOURCES + ["jordan_ref"]

# kream.brand LIKE 매핑 — retail source 명 ↔ kream brand 키워드
BRAND_LIKE: dict[str, list[str]] = {
    "nike": ["Nike"],
    "adidas": ["Adidas"],
    "nbkorea": ["New Balance"],
    "vans": ["Vans"],
    "asics": ["Asics"],
    "jordan_ref": ["Jordan"],  # retail source 는 없음, 참고용 kream 집계만
}

_PREFIX_LEN = 6


def _kream_cover(db_path: str, brand_keywords: list[str], kream_strip_set: set[str]) -> dict:
    """크림 `brand` 상품 중 retail_products 에 exact match 되는 비율."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = " OR ".join(["brand LIKE ?"] * len(brand_keywords))
        params = tuple(f"%{k}%" for k in brand_keywords)
        rows = conn.execute(
            f"SELECT model_number FROM kream_products "
            f"WHERE ({placeholders}) AND model_number != ''",
            params,
        ).fetchall()
    finally:
        conn.close()
    kream_total = len(rows)
    if not kream_total:
        return {"kream_total": 0, "matched": 0, "cover_pct": None}
    # retail 쪽 strip set 을 읽는 대신 kream_strip_set (retail keys) 로 역조회
    # (kream_strip_set 은 get_kream_index 리턴이라 kream 측 키다 — retail set 새로 빌드 필요)
    # → 이 함수는 호출자에서 retail_strip_set 을 넘기도록 refactor 할 수 있지만,
    #   간단히 inline 에서 retail 스캔 (1회, 캐시됨)
    return _kream_cover_impl(db_path, brand_keywords, kream_total, rows)


_RETAIL_STRIP_CACHE: set[str] | None = None


def _retail_strip_set(db_path: str) -> set[str]:
    global _RETAIL_STRIP_CACHE
    if _RETAIL_STRIP_CACHE is not None:
        return _RETAIL_STRIP_CACHE
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT model_number FROM retail_products WHERE model_number != ''"
        ).fetchall()
    finally:
        conn.close()
    _RETAIL_STRIP_CACHE = {strip_key(r[0]) for r in rows}
    return _RETAIL_STRIP_CACHE


def _kream_cover_impl(
    db_path: str, brand_keywords: list[str], kream_total: int, kream_rows: list
) -> dict:
    retail_keys = _retail_strip_set(db_path)
    matched = sum(1 for r in kream_rows if strip_key(r["model_number"]) in retail_keys)
    return {
        "kream_total": kream_total,
        "matched": matched,
        "cover_pct": matched / kream_total * 100.0 if kream_total else None,
    }


def _classify_sample(
    sample: dict,
    kream_strip_sorted: list[str],
) -> dict:
    model_no = sample.get("model_no", "") or ""
    name = sample.get("name", "") or ""
    url = sample.get("url", "") or ""

    model_stripped = strip_key(model_no) if model_no else ""
    extracted = extract_model_from_name(name)
    extracted_stripped = strip_key(extracted) if extracted else ""

    # 핵심 체크
    model_in_kream = bool(model_stripped) and _in_sorted(kream_strip_sorted, model_stripped)
    extracted_in_kream = bool(extracted_stripped) and _in_sorted(
        kream_strip_sorted, extracted_stripped
    )

    # prefix 매칭 (색상 suffix 없는 케이스)
    prefix = model_stripped[:_PREFIX_LEN]
    kream_prefix_hit = bool(prefix) and len(prefix) >= 4 and _prefix_hit(
        kream_strip_sorted, prefix
    )

    # 가드 키워드
    lowered = name.lower()
    collab_hit = any(kw in lowered for kw in COLLAB_KEYWORDS)
    subtype_hit = any(kw in lowered for kw in SUBTYPE_KEYWORDS)

    # bucket 결정 (우선순위 D > C > B > A > E)
    if model_in_kream or extracted_in_kream:
        # 실제론 크림에 있는데 orchestrator 가 실패? 가드 차단 의심
        bucket = "D" if (collab_hit or subtype_hit) else "E"
    elif kream_prefix_hit:
        bucket = "C"
    elif extracted and not extracted_in_kream:
        bucket = "B"
    elif not extracted:
        bucket = "A"
    else:
        bucket = "E"

    return {
        "model_no": model_no,
        "name": name,
        "url": url,
        "extracted": extracted or "",
        "model_in_kream": model_in_kream,
        "extracted_in_kream": extracted_in_kream,
        "kream_prefix_hit": kream_prefix_hit,
        "collab_hit": collab_hit,
        "subtype_hit": subtype_hit,
        "bucket": bucket,
    }


def _in_sorted(sorted_keys: list[str], key: str) -> bool:
    i = bisect.bisect_left(sorted_keys, key)
    return i < len(sorted_keys) and sorted_keys[i] == key


def _prefix_hit(sorted_keys: list[str], prefix: str) -> bool:
    i = bisect.bisect_left(sorted_keys, prefix)
    return i < len(sorted_keys) and sorted_keys[i].startswith(prefix)


def _decision_log_reason_dist(db_path: str, source: str) -> list[tuple[str, int]]:
    """최근 24h decision_log 에서 source 별 reason 빈도."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """SELECT reason, COUNT(*) AS n FROM decision_log
               WHERE source = ? AND ts > strftime('%s','now','-1 day')
               GROUP BY reason ORDER BY n DESC LIMIT 15""",
            (source,),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        # ts 가 문자열 타입인 스키마 fallback
        try:
            cur = conn.execute(
                """SELECT reason, COUNT(*) AS n FROM decision_log
                   WHERE source = ? AND ts > datetime('now','-1 day')
                   GROUP BY reason ORDER BY n DESC LIMIT 15""",
                (source,),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        conn.close()
    return [(r[0] or "", int(r[1])) for r in rows]


async def analyze_source(
    db_path: str,
    source: str,
    limit: int,
    kream_strip_sorted: list[str],
) -> dict:
    result: dict = {"source": source}

    # 덤프 측 match_rate (retail source 없는 jordan_ref 는 스킵)
    if source != "jordan_ref":
        result["dump_match_rate"] = await match_rate(db_path, source)
        raw_samples = await unmatched_sample(db_path, source, limit=limit)
    else:
        result["dump_match_rate"] = {
            "source": source, "dumped": 0, "matched": 0, "match_rate_pct": None,
        }
        raw_samples = []

    # 크림 측 cover율
    result["kream_cover"] = _kream_cover(db_path, BRAND_LIKE[source], set())

    # 샘플 bucket 분류
    classified = [_classify_sample(s, kream_strip_sorted) for s in raw_samples]
    result["samples"] = classified

    bucket_hist: dict[str, int] = {b: 0 for b in "ABCDE"}
    for c in classified:
        bucket_hist[c["bucket"]] = bucket_hist.get(c["bucket"], 0) + 1
    result["buckets"] = bucket_hist

    # decision_log reason
    result["reasons"] = _decision_log_reason_dist(db_path, source) if source != "jordan_ref" else []

    return result


def _fmt_pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "-"


def _render_md(results: list[dict]) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    out: list[str] = []
    out.append(f"# Coverage Diagnostics — {ts}")
    out.append("")
    out.append("## 요약")
    out.append("")
    out.append(
        "| source | dumped | dump_m | dump_rate | kream | kream_m | cover | A/B/C/D/E |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in results:
        dmr = r["dump_match_rate"]
        kc = r["kream_cover"]
        buckets = r["buckets"]
        bk_str = "/".join(str(buckets.get(b, 0)) for b in "ABCDE")
        out.append(
            f"| {r['source']} | {dmr['dumped']} | {dmr['matched']} "
            f"| {_fmt_pct(dmr['match_rate_pct'])} | {kc['kream_total']} "
            f"| {kc['matched']} | {_fmt_pct(kc['cover_pct'])} | {bk_str} |"
        )
    out.append("")

    for r in results:
        out.append(f"## {r['source']}")
        out.append("")
        dmr = r["dump_match_rate"]
        kc = r["kream_cover"]
        dump_rate = _fmt_pct(dmr["match_rate_pct"])
        cover = _fmt_pct(kc["cover_pct"])
        out.append(
            f"- 덤프: dumped **{dmr['dumped']}** / matched "
            f"**{dmr['matched']}** / rate **{dump_rate}**"
        )
        out.append(
            f"- 크림: kream **{kc['kream_total']}** / matched "
            f"**{kc['matched']}** / cover **{cover}**"
        )
        out.append(f"- bucket: {r['buckets']}")
        if r["reasons"]:
            out.append("- decision_log 최근 24h:")
            for reason, n in r["reasons"]:
                out.append(f"  - `{reason}`: {n}")
        else:
            out.append("- decision_log 최근 24h: (없음)")
        out.append("")
        if r["samples"]:
            out.append("### unmatched 샘플")
            out.append("")
            out.append(
                "| bkt | model_no | extracted | in_k | pfx | col | sub | name |"
            )
            out.append("|---|---|---|:-:|:-:|:-:|:-:|---|")
            for s in r["samples"]:
                name = (s["name"] or "").replace("|", "\\|")[:60]
                ext = s["extracted"] or "-"
                in_k = "✓" if s["model_in_kream"] else ("ex" if s["extracted_in_kream"] else "-")
                out.append(
                    f"| {s['bucket']} | `{s['model_no']}` | `{ext}` "
                    f"| {in_k} "
                    f"| {'✓' if s['kream_prefix_hit'] else '-'} "
                    f"| {'✓' if s['collab_hit'] else '-'} "
                    f"| {'✓' if s['subtype_hit'] else '-'} "
                    f"| {name} |"
                )
            out.append("")
    return "\n".join(out)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--brand",
        choices=SOURCES_WITH_EXTRA + ["all"],
        default="all",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default=settings.db_path)
    parser.add_argument("--out-dir", default="data/diag")
    args = parser.parse_args()

    if args.brand == "all":
        sources = SOURCES_WITH_EXTRA
    else:
        sources = [args.brand]

    # kream index (strip set)
    kream_index = get_kream_index(args.db).get()
    kream_strip_sorted = sorted(kream_index.keys())
    print(f"[kream_index] {len(kream_strip_sorted):,}건 strip-key 로드")

    results = []
    for src in sources:
        print(f"  ... {src}", flush=True)
        r = await analyze_source(args.db, src, args.limit, kream_strip_sorted)
        results.append(r)

    # 출력
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"coverage_{ts}.jsonl"
    md_path = out_dir / f"coverage_{ts}.md"

    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in results:
            for s in r["samples"]:
                row = {"source": r["source"], **s}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    md_path.write_text(_render_md(results), encoding="utf-8")

    # console 요약
    print("\n" + "=" * 72)
    print(" Coverage Diagnostics 결과")
    print("=" * 72)
    header = (
        f" {'source':10s} {'dump':>6}/{'m':>6} {'rate':>6}  "
        f"{'kream':>6}/{'m':>6} {'cov':>6}  A/B/C/D/E"
    )
    print(header)
    for r in results:
        dmr = r["dump_match_rate"]
        kc = r["kream_cover"]
        buckets = r["buckets"]
        bk_str = "/".join(str(buckets.get(b, 0)) for b in "ABCDE")
        rate = _fmt_pct(dmr["match_rate_pct"])
        cover = _fmt_pct(kc["cover_pct"])
        print(
            f" {r['source']:10s} {dmr['dumped']:>6}/{dmr['matched']:>6} "
            f"{rate:>6}  {kc['kream_total']:>6}/{kc['matched']:>6} "
            f"{cover:>6}  {bk_str}"
        )
    print(f"\n 💾 jsonl: {jsonl_path}")
    print(f" 💾 md:    {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
