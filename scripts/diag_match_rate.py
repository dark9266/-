#!/usr/bin/env python3
"""매칭률 9건 고착 원인 진단.

push_dump_full.py의 Phase 1+2+3을 재현하되, 매 상품 단위로 다음을 JSONL에 기록:
- brand / brandName / goodsName / price
- extracted_model_from_name
- detail.model_number (상세 fetch 결과)
- kream 매칭 여부 (hit / name_miss / detail_miss / brand_no_model)

출력: data/pilot_results/diag_match_rate_<ts>.jsonl (라인당 1개)

크림 API 호출 없음. 무신사만 사용 → 안전.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.crawlers.musinsa_httpx import musinsa_crawler
from src.matcher import normalize_model_number

# push_dump_full.py에서 복붙 — 가볍게 유지
PB_BLACKLIST_SLUGS = {
    "musinsastandard", "musinsa", "muztd", "muztdstandard", "mmlg",
    "musinsamen", "musinsawomen", "musinsasports", "musinsabasic",
    "musinsacollection", "musinsaedition", "musinsaoriginal", "musinsaoutdoor",
}
CATEGORIES_FULL = {
    "103": "신발", "001": "상의", "002": "아우터", "003": "바지",
    "004": "가방", "017": "스포츠/레저", "105": "부티크", "101": "소품",
}
CATEGORIES_CORE = {
    "103": "신발", "001": "상의", "002": "아우터", "003": "바지",
    "004": "가방", "017": "스포츠/레저",
}
_MODEL_PAT_IN_NAME = re.compile(r"/\s*([A-Z0-9][A-Z0-9\-]{4,})\s*$")


def _slug(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").lower())


def _strip_key(mn: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(mn))


def _is_pb(brand: str, brand_name: str) -> bool:
    a, b = _slug(brand), _slug(brand_name)
    return any(pb and (pb in a or pb in b) for pb in PB_BLACKLIST_SLUGS)


def _extract_model_from_name(name: str) -> str:
    if not name:
        return ""
    m = _MODEL_PAT_IN_NAME.search(name)
    return m.group(1).strip() if m else ""


def _load_kream(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT product_id, name, brand, model_number FROM kream_products "
            "WHERE model_number != ''"
        ).fetchall()
        brand_rows = conn.execute(
            "SELECT DISTINCT brand FROM kream_products WHERE brand != ''"
        ).fetchall()
    finally:
        conn.close()
    idx: dict[str, dict] = {}
    for r in rows:
        k = _strip_key(r["model_number"])
        if k:
            idx[k] = dict(r)
    slugs: set[str] = set()
    for (b,) in brand_rows:
        s = _slug(b)
        if s:
            slugs.add(s)
        compact = re.sub(r"[^a-z0-9]", "", (b or "").lower())
        if compact:
            slugs.add(compact)
    return idx, slugs


def _brand_in_kream(brand: str, brand_name: str, slugs: set[str]) -> bool:
    a, b = _slug(brand), _slug(brand_name)
    for cand in (a, b):
        if not cand:
            continue
        if cand in slugs:
            return True
        for ks in slugs:
            if len(ks) >= 4 and (ks in cand or cand in ks):
                return True
    return False


async def _fetch(sem, goods_no):
    async with sem:
        try:
            return goods_no, await musinsa_crawler.get_product_detail(goods_no)
        except Exception as e:
            return goods_no, e


async def main(max_pages: int, categories: dict[str, str]) -> None:
    t0 = time.perf_counter()
    print(f"[Phase 0] 크림 인덱스 로드")
    idx, slugs = _load_kream(settings.db_path)
    print(f"  ✓ 상품 {len(idx):,}건 / 브랜드 slug {len(slugs)}개")

    print(f"\n[Phase 1] 카테고리 덤프 ({len(categories)}개 × {max_pages}페이지)")
    listing: list[dict] = []
    for code, name in categories.items():
        print(f"  • {code} {name} ...", end=" ", flush=True)
        try:
            items = await musinsa_crawler.fetch_category_listing(code, max_pages=max_pages)
        except Exception as e:
            print(f"ERR {e}")
            continue
        for it in items:
            it["_cat_code"] = code
            it["_cat_name"] = name
        listing.extend(items)
        print(f"{len(items)}건")
    print(f"  ✓ 총 {len(listing)}건")

    # 진단 레코드 누적
    records: list[dict] = []
    brand_stats: dict[str, dict] = {}

    def bs(brand: str) -> dict:
        return brand_stats.setdefault(
            brand or "UNKNOWN",
            {"total": 0, "passed_whitelist": 0, "name_hit": 0, "detail_hit": 0, "detail_miss": 0},
        )

    passed: list[dict] = []
    for item in listing:
        brand = item.get("brand") or item.get("brandName") or ""
        rec = {
            "goods_no": item.get("goodsNo"),
            "brand": item.get("brand"),
            "brand_name": item.get("brandName"),
            "goods_name": item.get("goodsName"),
            "price": item.get("price"),
            "cat": item.get("_cat_code"),
            "is_soldout": item.get("isSoldOut"),
            "is_pb": False,
            "brand_in_kream": False,
            "model_from_name": "",
            "name_kream_hit": False,
            "detail_model": None,
            "detail_kream_hit": False,
            "stage": "",
        }
        bs(brand)["total"] += 1
        if item.get("isSoldOut"):
            rec["stage"] = "soldout"
            records.append(rec)
            continue
        if _is_pb(item.get("brand", ""), item.get("brandName", "")):
            rec["is_pb"] = True
            rec["stage"] = "pb"
            records.append(rec)
            continue
        if not _brand_in_kream(item.get("brand", ""), item.get("brandName", ""), slugs):
            rec["stage"] = "brand_not_kream"
            records.append(rec)
            continue
        rec["brand_in_kream"] = True
        bs(brand)["passed_whitelist"] += 1
        mn = _extract_model_from_name(item.get("goodsName", ""))
        rec["model_from_name"] = mn
        if mn:
            k = _strip_key(mn)
            if k in idx:
                rec["name_kream_hit"] = True
                rec["stage"] = "name_hit"
                bs(brand)["name_hit"] += 1
                records.append(rec)
                continue
            else:
                # 모델이 있는데 크림에 없음 — 상세로는 갈 필요 없지만
                # 진단을 위해 상세 fetch해서 진짜 미스인지 확인
                rec["stage"] = "name_miss"
                records.append(rec)
                passed.append(item)
                continue
        rec["stage"] = "need_detail"
        records.append(rec)
        passed.append(item)

    # rec_by_goods for later update
    rec_by_goods = {r["goods_no"]: r for r in records}

    # 진단 모드: 화이트리스트 통과 전부 상세 fetch (기존 로직은 name match 있으면 스킵)
    print(f"\n[Phase 3] 상세 fetch {len(passed)}건 (진단용 — 전부 fetch)")
    sem = asyncio.Semaphore(8)
    tasks = [_fetch(sem, str(it.get("goodsNo"))) for it in passed if it.get("goodsNo")]
    done = await asyncio.gather(*tasks) if tasks else []
    ok = 0
    for goods_no, res in done:
        rec = rec_by_goods.get(goods_no)
        if rec is None:
            continue
        if isinstance(res, Exception) or res is None:
            rec["detail_model"] = f"ERR:{type(res).__name__}" if isinstance(res, Exception) else "None"
            continue
        ok += 1
        rec["detail_model"] = res.model_number or ""
        if not res.model_number:
            bs(rec["brand"] or "UNKNOWN")["detail_miss"] += 1
            continue
        k = _strip_key(res.model_number)
        if k in idx:
            rec["detail_kream_hit"] = True
            bs(rec["brand"] or "UNKNOWN")["detail_hit"] += 1
        else:
            bs(rec["brand"] or "UNKNOWN")["detail_miss"] += 1
    print(f"  ✓ 상세 성공 {ok}/{len(tasks)}")

    # 저장
    out_dir = Path("data/pilot_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = out_dir / f"diag_match_rate_{ts}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 집계
    total = len(listing)
    passed_wl = sum(1 for r in records if r["brand_in_kream"])
    name_hits = sum(1 for r in records if r["name_kream_hit"])
    detail_hits = sum(1 for r in records if r["detail_kream_hit"])

    print("\n" + "=" * 64)
    print(" 📊 매칭률 진단")
    print("=" * 64)
    print(f"  덤프 총량          : {total}")
    print(f"  화이트리스트 통과  : {passed_wl}")
    print(f"    ㄴ 이름 매칭     : {name_hits}")
    print(f"    ㄴ 상세 매칭     : {detail_hits}")
    print(f"    ㄴ 매칭 실패     : {passed_wl - name_hits - detail_hits}")
    print()
    print(" 브랜드별 (whitelist 통과 TOP 20, 매칭률 순):")
    top = sorted(
        ((b, s) for b, s in brand_stats.items() if s["passed_whitelist"] > 0),
        key=lambda kv: kv[1]["passed_whitelist"],
        reverse=True,
    )[:20]
    print(f"  {'brand':25s} {'통과':>5} {'이름히트':>7} {'상세히트':>7} {'상세미스':>7}")
    for b, s in top:
        print(f"  {b[:25]:25s} {s['passed_whitelist']:>5} {s['name_hit']:>7} {s['detail_hit']:>7} {s['detail_miss']:>7}")

    print(f"\n  💾 raw: {jsonl_path}")
    print(f"  총 소요: {time.perf_counter() - t0:.1f}s")

    try:
        await musinsa_crawler.close()
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--scope", choices=["core", "full"], default="full")
    args = parser.parse_args()
    cats = CATEGORIES_FULL if args.scope == "full" else CATEGORIES_CORE
    asyncio.run(main(args.pages, cats))
