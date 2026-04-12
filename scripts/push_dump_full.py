#!/usr/bin/env python3
"""무신사 전체 카탈로그 덤프 → 크림 DB 매칭 → 수익 기회 발굴.

pilot_musinsa_push.py(신발 전용)의 확장판.

변경점:
- 카테고리 여러 개 순회 (신발/상의/아우터/바지/가방/스포츠)
- 필터 반전: 크림 브랜드 화이트리스트 → 무신사 PB 블랙리스트
- 리스팅 응답의 goodsName에서 모델번호 선(先)파싱 → 상세 호출 회피
- Phase 3 상세 fetch 병렬화 (Semaphore 8)

실행:
    PYTHONPATH=. python scripts/push_dump_full.py
    PYTHONPATH=. python scripts/push_dump_full.py --pages 10
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
from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa_httpx import musinsa_crawler
from src.matcher import normalize_model_number
from src.profit_calculator import calculate_size_profit

# ─── 스캔 대상 카테고리 ───────────────────────────────────────────
# 무신사 대분류 — Kream 취급 품목과 겹치는 영역만
# 출처: 무신사 /api2/dp/v1/categories (17개) × 크림 8개 카테고리 (신발/상의/가방/액세서리/아우터/하의/모자/sneakers)
# 제외 근거:
#   104 뷰티 / 026 속옷 / 102 디지털 / 106 키즈 / 111 K커넥트 / 109 유즈드 / 107 아울렛(중복) / 108 어스
#   100 원피스·스커트 — 크림에 해당 카테고리 없음
CATEGORIES_FULL: dict[str, str] = {
    "103": "신발",
    "001": "상의",
    "002": "아우터",
    "003": "바지",
    "004": "가방",
    "017": "스포츠/레저",
    "105": "부티크",       # LV 2499 / Chanel 2476 / Chrome Hearts 1100 → 크림 가방·액세서리 매칭
    "101": "소품",         # 크림 액세서리 4605 + 모자 1763 = 6368건, 커버리지 갭 가장 큼
}

# 1차 검증용 — 주력 6개
CATEGORIES_CORE: dict[str, str] = {
    "103": "신발",
    "001": "상의",
    "002": "아우터",
    "003": "바지",
    "004": "가방",
    "017": "스포츠/레저",
}

# ─── 자체브랜드 블랙리스트 ────────────────────────────────────────
# 무신사 PB (brand/brandName slug 기준, 부분일치)
PB_BLACKLIST_SLUGS = {
    "musinsastandard",
    "musinsa",
    "muztd",  # 무탠다드 별칭
    "muztdstandard",
    "mmlg",
    "musinsamen",
    "musinsawomen",
    "musinsasports",
    "musinsabasic",
    "musinsacollection",
    "musinsaedition",
    "musinsaoriginal",
    "musinsaoutdoor",
}

MIN_NET_PROFIT = 10_000
MIN_ROI_PERCENT = 5.0
DETAIL_CONCURRENCY = 8

# ─── 크림 실계정 안전장치 ─────────────────────────────────────────
# 1회 실행에서 호출 가능한 크림 API 최대 횟수. 초과시 Phase 5 중단.
MAX_KREAM_CALLS_DEFAULT = 150
# Kream 에러 조기 중단 임계값 (연속 에러)
KREAM_ERROR_ABORT_THRESHOLD = 5


def _slug(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").lower())


def _is_pb(brand: str, brand_name: str) -> bool:
    a, b = _slug(brand), _slug(brand_name)
    for pb in PB_BLACKLIST_SLUGS:
        if pb and (pb in a or pb in b):
            return True
    return False


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


_MODEL_PAT_IN_NAME = re.compile(r"/\s*([A-Z0-9][A-Z0-9\-]{4,})\s*$")


def _extract_model_from_name(name: str) -> str:
    """상품명 끝 `/ MODEL-NUMBER` 패턴에서 모델번호 추출."""
    if not name:
        return ""
    m = _MODEL_PAT_IN_NAME.search(name)
    return m.group(1).strip() if m else ""


def _load_kream_index(db_path: str) -> dict[str, dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT product_id, name, brand, model_number FROM kream_products "
            "WHERE model_number != ''"
        ).fetchall()
    finally:
        conn.close()
    index: dict[str, dict] = {}
    for row in rows:
        key = _strip_key(row["model_number"])
        if key:
            index[key] = dict(row)
    return index


def _load_kream_brand_slugs(db_path: str) -> set[str]:
    """크림 DB 전체 브랜드를 정규화 slug 집합으로 반환.

    화이트리스트 모드에서 Phase 2에 적용 — 무신사 brand가 이 집합과 매칭되지
    않으면 상세 fetch 스킵.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT brand FROM kream_products WHERE brand != ''"
        ).fetchall()
    finally:
        conn.close()
    slugs: set[str] = set()
    for (brand,) in rows:
        s = _slug(brand)
        if s:
            slugs.add(s)
        # "New Balance" → "newbalance" 같은 공백 변형 추가 대응
        compact = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
        if compact:
            slugs.add(compact)
    return slugs


def _brand_in_kream(brand: str, brand_name: str, kream_slugs: set[str]) -> bool:
    """무신사 brand/brandName이 크림 브랜드 집합과 부분일치하는지."""
    a = _slug(brand)
    b = _slug(brand_name)
    for cand in (a, b):
        if not cand:
            continue
        if cand in kream_slugs:
            return True
        # 부분일치: "nikekorea" → "nike" 포함 여부
        for ks in kream_slugs:
            if len(ks) >= 4 and (ks in cand or cand in ks):
                return True
    return False


async def _dump_category(cat_code: str, cat_name: str, max_pages: int) -> list[dict]:
    try:
        items = await musinsa_crawler.fetch_category_listing(cat_code, max_pages=max_pages)
    except Exception as e:
        print(f"  ✖ [{cat_code} {cat_name}] 리스팅 실패: {e}")
        return []
    for it in items:
        it["_category_code"] = cat_code
        it["_category_name"] = cat_name
    return items


async def _fetch_detail_bounded(sem: asyncio.Semaphore, goods_no: str):
    async with sem:
        try:
            return goods_no, await musinsa_crawler.get_product_detail(goods_no)
        except Exception as e:
            return goods_no, e


async def main(
    max_pages: int,
    categories: dict[str, str],
    max_kream_calls: int,
    brand_filter_mode: str,
) -> None:
    """brand_filter_mode: 'blacklist' (PB만 제외) | 'whitelist' (크림 브랜드만 통과)"""
    results: dict = {
        "started_at": datetime.now().isoformat(),
        "max_pages_per_category": max_pages,
        "max_kream_calls": max_kream_calls,
        "brand_filter_mode": brand_filter_mode,
        "categories": list(categories.keys()),
        "phase_timings": {},
        "counts": {},
        "per_category": {},
        "opportunities": [],
        "errors": [],
    }

    t0 = time.perf_counter()

    # Phase 0: 크림 인덱스
    print("\n[Phase 0] 크림 DB 인덱스 로드")
    t_a = time.perf_counter()
    kream_index = _load_kream_index(settings.db_path)
    kream_brand_slugs: set[str] = set()
    if brand_filter_mode == "whitelist":
        kream_brand_slugs = _load_kream_brand_slugs(settings.db_path)
    results["phase_timings"]["kream_index_load"] = round(time.perf_counter() - t_a, 3)
    results["counts"]["kream_db_indexed"] = len(kream_index)
    results["counts"]["kream_brand_count"] = len(kream_brand_slugs)
    print(f"  ✓ 상품 {len(kream_index):,}건 / 브랜드 slug {len(kream_brand_slugs)}개 ({brand_filter_mode})")

    # Phase 1: 카테고리별 리스팅 덤프 (순차 — Musinsa rate limiter 내부 제어)
    print(f"\n[Phase 1] 카테고리 덤프 ({len(categories)}개 × 최대 {max_pages}페이지)")
    t_a = time.perf_counter()
    listing: list[dict] = []
    for code, name in categories.items():
        print(f"  • {code} {name} ...", end=" ", flush=True)
        items = await _dump_category(code, name, max_pages)
        listing.extend(items)
        results["per_category"].setdefault(code, {})["name"] = name
        results["per_category"][code]["dumped"] = len(items)
        print(f"{len(items)}건")
    results["phase_timings"]["listing_dump"] = round(time.perf_counter() - t_a, 2)
    results["counts"]["listing_dumped"] = len(listing)
    print(f"  ✓ 총 덤프 {len(listing)}건")

    # Phase 2: 브랜드 필터 + 품절 제외 + 이름에서 모델번호 선파싱
    print(f"\n[Phase 2] 브랜드 필터({brand_filter_mode}) + 재고 + 이름 모델번호 선파싱")
    t_a = time.perf_counter()
    pre_matched: list[tuple[dict, dict, str]] = []  # (item, kream_row, model_from_name)
    need_detail: list[dict] = []
    pb_dropped = 0
    soldout_dropped = 0
    brand_filtered = 0  # whitelist 모드에서 크림 브랜드 미매칭으로 드랍
    for item in listing:
        if item.get("isSoldOut"):
            soldout_dropped += 1
            continue
        if _is_pb(item.get("brand", ""), item.get("brandName", "")):
            pb_dropped += 1
            continue
        if brand_filter_mode == "whitelist":
            if not _brand_in_kream(
                item.get("brand", ""),
                item.get("brandName", ""),
                kream_brand_slugs,
            ):
                brand_filtered += 1
                continue
        mn = _extract_model_from_name(item.get("goodsName", ""))
        if mn:
            key = _strip_key(mn)
            kr = kream_index.get(key)
            if kr:
                pre_matched.append((item, kr, mn))
                continue
            # 이름에 모델번호가 있었지만 크림 DB에 없음 → 상세 스킵 (높은 확률로 비크림)
            continue
        need_detail.append(item)
    results["phase_timings"]["filter_prematch"] = round(time.perf_counter() - t_a, 3)
    results["counts"]["pb_dropped"] = pb_dropped
    results["counts"]["soldout_dropped"] = soldout_dropped
    results["counts"]["brand_whitelist_dropped"] = brand_filtered
    results["counts"]["prematched_by_name"] = len(pre_matched)
    results["counts"]["needs_detail_fetch"] = len(need_detail)
    print(f"  ✓ PB 제외 {pb_dropped}, 품절 제외 {soldout_dropped}, 크림 브랜드 미매칭 제외 {brand_filtered}")
    print(f"  ✓ 이름 선매칭 {len(pre_matched)}건, 상세 필요 {len(need_detail)}건")

    # Phase 3: 상세 API 병렬 fetch (이름 파싱 실패분만)
    print(f"\n[Phase 3] 상세 API 병렬 fetch (concurrency={DETAIL_CONCURRENCY})")
    t_a = time.perf_counter()
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    tasks = [
        _fetch_detail_bounded(sem, str(it.get("goodsNo")))
        for it in need_detail
        if it.get("goodsNo")
    ]
    done = await asyncio.gather(*tasks) if tasks else []
    detail_matched: list[tuple[object, dict]] = []  # (RetailProduct, kream_row)
    detail_unmatched: list[object] = []  # 모델번호는 있는데 크림 DB에 없음 → 큐 적재 후보
    fetch_ok = 0
    for goods_no, res in done:
        if isinstance(res, Exception):
            results["errors"].append(f"detail[{goods_no}]: {type(res).__name__}")
            continue
        if res is None:
            continue
        fetch_ok += 1
        if not res.model_number:
            continue
        key = _strip_key(res.model_number)
        if not key:
            continue
        kr = kream_index.get(key)
        if kr:
            detail_matched.append((res, kr))
        else:
            detail_unmatched.append(res)
    results["phase_timings"]["detail_fetch"] = round(time.perf_counter() - t_a, 2)
    results["counts"]["details_ok"] = fetch_ok
    results["counts"]["matched_via_detail"] = len(detail_matched)
    total_matched = len(pre_matched) + len(detail_matched)
    results["counts"]["matched_to_kream"] = total_matched
    print(f"  ✓ 상세 수집 {fetch_ok}/{len(tasks)}, 추가 매칭 {len(detail_matched)}건")
    print(f"  ✓ 총 매칭 {total_matched}건 (선매칭 {len(pre_matched)} + 상세 {len(detail_matched)})")

    # Phase 4: 미등재 신상품 큐 enqueue
    # 크림 미등재 = 푸시의 진짜 가치 = 조기경보 신호
    # collector.collect_pending()이 주기적으로 큐를 처리해 등재되면 DB에 추가
    print(f"\n[Phase 4] 크림 미등재 신상품 큐 enqueue")
    t_a = time.perf_counter()
    enqueued = 0
    skipped_existing = 0
    if detail_unmatched:
        conn = sqlite3.connect(settings.db_path)
        try:
            for retail in detail_unmatched:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO kream_collect_queue
                    (model_number, brand_hint, name_hint, source, source_url)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        retail.model_number,
                        getattr(retail, "brand", "") or "",
                        getattr(retail, "name", "") or "",
                        getattr(retail, "source", "musinsa"),
                        getattr(retail, "url", "") or "",
                    ),
                )
                if cur.rowcount > 0:
                    enqueued += 1
                else:
                    skipped_existing += 1
            conn.commit()
        finally:
            conn.close()
    results["phase_timings"]["enqueue"] = round(time.perf_counter() - t_a, 3)
    results["counts"]["enqueued_new"] = enqueued
    results["counts"]["enqueue_skipped_existing"] = skipped_existing
    results["counts"]["detail_unmatched_total"] = len(detail_unmatched)
    print(f"  ✓ 큐 신규 {enqueued}건, 중복 스킵 {skipped_existing}건 / 후보 {len(detail_unmatched)}건")

    # 이름 선매칭 항목은 상세가 없어 크림 실시간 수익 계산 스킵.
    # 1차 전체 덤프 가설 검증이 목표이므로 detail_matched 만 Phase 5에 투입.
    # (향후: 선매칭 항목도 사이즈 맞추려면 상세 추가 호출 필요)

    # Phase 5: 크림 sell_now + 수익 계산 (detail_matched만)
    # 실계정 보호: MAX_KREAM_CALLS 하드캡 + 연속 에러 조기 중단
    planned = min(len(detail_matched), max_kream_calls)
    print(f"\n[Phase 5] 크림 sell_now + 수익 계산 — 대상 {len(detail_matched)}건, 실행 상한 {planned}건")
    if len(detail_matched) > max_kream_calls:
        print(f"  ⚠ 매칭 {len(detail_matched)}건 > 상한 {max_kream_calls}건 — 상위 {max_kream_calls}건만 조회")
    t_a = time.perf_counter()
    kream_ok = 0
    kream_calls_made = 0
    consecutive_errors = 0
    aborted = False
    opportunities: list[dict] = []
    for retail, kream_row in detail_matched[:max_kream_calls]:
        pid = kream_row["product_id"]
        kream_calls_made += 1
        try:
            size_prices = await kream_crawler.get_sell_prices(pid)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            err_name = type(e).__name__
            results["errors"].append(f"kream_sell[{pid}]: {err_name}")
            if consecutive_errors >= KREAM_ERROR_ABORT_THRESHOLD:
                print(f"  🛑 크림 연속 에러 {consecutive_errors}회 — 실계정 보호 위해 Phase 5 중단")
                results["errors"].append(f"aborted_consecutive_errors={consecutive_errors}")
                aborted = True
                break
            continue
        if not size_prices:
            continue
        kream_ok += 1

        retail_size_map: dict[str, tuple[int, bool]] = {}
        for rs in retail.sizes:
            if rs.price <= 0:
                continue
            retail_size_map[rs.size.strip()] = (rs.price, rs.in_stock)

        for ksize in size_prices:
            if not ksize.sell_now_price or ksize.sell_now_price <= 0:
                continue
            match = retail_size_map.get(ksize.size.strip())
            if not match:
                continue
            retail_price, in_stock = match
            if not in_stock:
                continue

            prof = calculate_size_profit(
                retail_price=retail_price,
                kream_sell_price=ksize.sell_now_price,
                in_stock=True,
                bid_count=ksize.bid_count or 0,
            )
            if prof.net_profit >= MIN_NET_PROFIT and prof.roi >= MIN_ROI_PERCENT:
                opportunities.append({
                    "kream_product_id": pid,
                    "kream_name": kream_row["name"],
                    "brand": kream_row["brand"],
                    "model_number": kream_row["model_number"],
                    "musinsa_goods_no": retail.product_id,
                    "musinsa_url": retail.url,
                    "size": ksize.size,
                    "retail_price": retail_price,
                    "kream_sell_price": ksize.sell_now_price,
                    "net_profit": prof.net_profit,
                    "roi": prof.roi,
                    "bid_count": ksize.bid_count,
                })

    results["phase_timings"]["kream_realtime"] = round(time.perf_counter() - t_a, 2)
    results["counts"]["kream_calls_made"] = kream_calls_made
    results["counts"]["kream_realtime_ok"] = kream_ok
    results["counts"]["kream_aborted"] = aborted
    results["counts"]["profitable_opportunities"] = len(opportunities)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)
    results["opportunities"] = opportunities
    print(f"  ✓ 크림 시세 {kream_ok}/{len(detail_matched)}, 수익 기회 {len(opportunities)}건")

    # 저장 + 요약
    total = time.perf_counter() - t0
    results["phase_timings"]["total"] = round(total, 2)
    results["finished_at"] = datetime.now().isoformat()

    out_dir = Path("data/pilot_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"push_dump_full_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    print("\n" + "=" * 64)
    print(" 📊 무신사 전체 덤프 결과")
    print("=" * 64)
    print(f" 카테고리 {len(categories)}개 × 최대 {max_pages}페이지 / 모드: {brand_filter_mode}")
    print(f" 크림 DB 인덱스: {results['counts']['kream_db_indexed']:,}건")
    print()
    print(" 파이프라인:")
    c = results["counts"]
    print(f"   덤프 총량:         {c['listing_dumped']:>6}건")
    print(f"   PB 제외:           {c['pb_dropped']:>6}건")
    print(f"   품절 제외:         {c['soldout_dropped']:>6}건")
    print(f"   브랜드 미매칭 제외:{c.get('brand_whitelist_dropped', 0):>6}건")
    print(f"   이름 선매칭:       {c['prematched_by_name']:>6}건")
    print(f"   상세 fetch 대상:   {c['needs_detail_fetch']:>6}건")
    print(f"   상세 성공:         {c['details_ok']:>6}건")
    print(f"   상세경로 매칭:     {c['matched_via_detail']:>6}건")
    print(f"   총 매칭:           {c['matched_to_kream']:>6}건")
    print(f"   미등재 큐 신규:    {c.get('enqueued_new', 0):>6}건 "
          f"(중복 {c.get('enqueue_skipped_existing', 0)})")
    print(f"   크림 시세 수집:    {c.get('kream_realtime_ok', 0):>6}건")
    print(f"   수익 기회:         {c['profitable_opportunities']:>6}건")
    print()
    print(" 단계별 소요:")
    for phase, sec in results["phase_timings"].items():
        print(f"   {phase:22s} {sec:>8.2f}s")
    print()
    print(" 카테고리별 덤프:")
    for code, info in results["per_category"].items():
        print(f"   {code} {info['name']:18s} {info['dumped']:>5}건")
    if opportunities:
        print(f"\n 🔥 수익 기회 TOP 10:")
        for i, opp in enumerate(opportunities[:10], 1):
            print(f"   {i:2d}. [{opp['brand']}] {opp['model_number']} {opp['size']}")
            print(f"       무신사 {opp['retail_price']:,}원 → 크림 {opp['kream_sell_price']:,}원"
                  f" / 순수익 {opp['net_profit']:,}원 ({opp['roi']}%)")
    else:
        print("\n (수익 임계값 충족 없음)")

    if results["errors"]:
        print(f"\n ⚠ 에러 {len(results['errors'])}건 (첫 5):")
        for err in results["errors"][:5]:
            print(f"   - {err}")

    print(f"\n 💾 결과: {out_file}")
    print("=" * 64)

    try:
        await musinsa_crawler.close()
    except Exception:
        pass
    try:
        await kream_crawler.close()
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=10,
                        help="카테고리별 최대 페이지 수 (기본 10 = 600건)")
    parser.add_argument("--scope", choices=["core", "full"], default="core",
                        help="core=6개 주력, full=8개 (부티크·소품 포함, 원피스 제외)")
    parser.add_argument("--mode", choices=["blacklist", "whitelist"], default="blacklist",
                        help="blacklist=PB만 제외 / whitelist=크림 브랜드만 통과 (Phase 3 부하 격감)")
    parser.add_argument("--max-kream-calls", type=int, default=MAX_KREAM_CALLS_DEFAULT,
                        help=f"Phase 5 크림 API 호출 상한 (기본 {MAX_KREAM_CALLS_DEFAULT}) — 실계정 보호")
    args = parser.parse_args()
    cats = CATEGORIES_FULL if args.scope == "full" else CATEGORIES_CORE
    asyncio.run(main(args.pages, cats, args.max_kream_calls, args.mode))
