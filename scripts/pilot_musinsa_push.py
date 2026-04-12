#!/usr/bin/env python3
"""푸시 방식 가설 검증 파일럿 — 무신사 카탈로그 덤프 → 크림 매칭 → 수익 계산.

목적:
    "무신사 카탈로그 통째로 긁어서 크림 DB 매칭 후 실시간 sell_now로 수익 기회 탐색"
    가설이 실제로 동작하는지, 어느 단계가 병목인지, 역방향 대비 어떤지를 실측.

실행:
    PYTHONPATH=. python scripts/pilot_musinsa_push.py

결과:
    콘솔 요약 + data/pilot_results/pilot_musinsa_*.json
"""

from __future__ import annotations

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

# ─── 파일럿 설정 ─────────────────────────────────────────────────
CATEGORY_SHOES = "103"  # 무신사 신발 카테고리
MAX_PAGES = 5  # 5 × 60 = 약 300건
MIN_NET_PROFIT = 10_000  # 알림 하드 플로어 (CLAUDE.md)
MIN_ROI_PERCENT = 5.0

# 크림에 존재하는 인기 브랜드 (소문자, 무공백). 미등록 브랜드는 상세 호출 스킵.
KREAM_BRANDS = {
    "nike", "jordan", "adidas", "newbalance", "nb",
    "puma", "reebok", "converse", "vans", "asics",
    "fila", "kangaroos", "salomon", "hoka", "arcteryx",
    "stussy", "supreme", "thenorthface", "carhartt", "nautica",
    "underarmour", "saucony", "onrunning", "on", "mizuno",
}


def _brand_slug(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").lower())


def _brand_match(*candidates: str) -> bool:
    for c in candidates:
        slug = _brand_slug(c)
        if not slug:
            continue
        for target in KREAM_BRANDS:
            if target in slug:
                return True
    return False


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _load_kream_index(db_path: str) -> dict[str, dict]:
    """kream_products를 (정규화 모델번호 → row) dict로 로드."""
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


async def main() -> None:
    results: dict = {
        "started_at": datetime.now().isoformat(),
        "category": CATEGORY_SHOES,
        "max_pages": MAX_PAGES,
        "phase_timings": {},
        "counts": {},
        "opportunities": [],
        "errors": [],
    }

    t0 = time.perf_counter()

    # =================================================================
    # Phase 0: 크림 DB 인덱스 사전 로드 (매칭용)
    # =================================================================
    print("\n[Phase 0] 크림 DB 인덱스 로드")
    t_idx = time.perf_counter()
    kream_index = _load_kream_index(settings.db_path)
    phase0_dur = time.perf_counter() - t_idx
    results["phase_timings"]["kream_index_load"] = round(phase0_dur, 3)
    results["counts"]["kream_db_indexed"] = len(kream_index)
    print(f"  ✓ {len(kream_index):,}건 인덱싱, {phase0_dur:.2f}초")

    # =================================================================
    # Phase 1: 무신사 카테고리 리스팅 (카탈로그 덤프)
    # =================================================================
    print(f"\n[Phase 1] 무신사 카테고리 {CATEGORY_SHOES} 리스팅 ({MAX_PAGES} 페이지)")
    t1 = time.perf_counter()
    try:
        listing = await musinsa_crawler.fetch_category_listing(
            CATEGORY_SHOES, max_pages=MAX_PAGES
        )
    except Exception as e:
        print(f"  ✖ 리스팅 실패: {e}")
        results["errors"].append(f"listing_fetch: {e}")
        await _cleanup()
        return
    phase1_dur = time.perf_counter() - t1
    results["phase_timings"]["listing_dump"] = round(phase1_dur, 2)
    results["counts"]["listing_dumped"] = len(listing)
    print(f"  ✓ {len(listing)}건 덤프, {phase1_dur:.1f}초")

    # =================================================================
    # Phase 2: 브랜드 필터 (크림 인기 브랜드만) + 품절 제외
    # =================================================================
    print("\n[Phase 2] 브랜드 필터 (크림 인기 브랜드 + 재고 O)")
    filtered = []
    for item in listing:
        if item.get("isSoldOut"):
            continue
        if not _brand_match(item.get("brandName"), item.get("brand")):
            continue
        filtered.append(item)
    results["counts"]["brand_filtered"] = len(filtered)
    print(f"  ✓ 필터 통과: {len(filtered)}건")

    # =================================================================
    # Phase 3: 상세 API로 모델번호 확보
    # =================================================================
    print(f"\n[Phase 3] 상세 API (모델번호 확보) — 대상 {len(filtered)}건")
    t3 = time.perf_counter()
    details = []
    model_ok = 0
    for i, item in enumerate(filtered, 1):
        goods_no = str(item.get("goodsNo") or "")
        if not goods_no:
            continue
        try:
            detail = await musinsa_crawler.get_product_detail(goods_no)
        except Exception as e:
            results["errors"].append(f"detail[{goods_no}]: {type(e).__name__}")
            continue
        if detail is None:
            continue
        details.append(detail)
        if detail.model_number:
            model_ok += 1
        if i % 20 == 0:
            print(f"    진행: {i}/{len(filtered)}")
    phase3_dur = time.perf_counter() - t3
    results["phase_timings"]["detail_fetch"] = round(phase3_dur, 2)
    results["counts"]["details_fetched"] = len(details)
    results["counts"]["details_with_model"] = model_ok
    print(f"  ✓ 상세 수집: {len(details)}건, 모델번호 확보: {model_ok}건, {phase3_dur:.1f}초")

    # =================================================================
    # Phase 4: 크림 DB 로컬 매칭 (API 0회, 사전 구축 인덱스)
    # =================================================================
    print("\n[Phase 4] 크림 DB 로컬 매칭")
    t4 = time.perf_counter()
    matched: list[tuple] = []  # (RetailProduct, kream_row)
    for retail in details:
        if not retail.model_number:
            continue
        key = _strip_key(retail.model_number)
        if not key:
            continue
        kream_row = kream_index.get(key)
        if kream_row:
            matched.append((retail, kream_row))
    phase4_dur = time.perf_counter() - t4
    results["phase_timings"]["local_matching"] = round(phase4_dur, 3)
    results["counts"]["matched_to_kream"] = len(matched)
    print(f"  ✓ 매칭: {len(matched)}건, {phase4_dur:.3f}초")

    # =================================================================
    # Phase 5: 크림 sell_now 실시간 조회 + 수익 계산 (매칭된 것만)
    # =================================================================
    print(f"\n[Phase 5] 크림 실시간 sell_now + 수익 계산 — 대상 {len(matched)}건")
    t5 = time.perf_counter()
    kream_ok = 0
    opportunities: list[dict] = []

    for retail, kream_row in matched:
        pid = kream_row["product_id"]
        try:
            size_prices = await kream_crawler.get_sell_prices(pid)
        except Exception as e:
            results["errors"].append(f"kream_sell[{pid}]: {type(e).__name__}")
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

    phase5_dur = time.perf_counter() - t5
    results["phase_timings"]["kream_realtime"] = round(phase5_dur, 2)
    results["counts"]["kream_realtime_ok"] = kream_ok
    results["counts"]["profitable_opportunities"] = len(opportunities)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)
    results["opportunities"] = opportunities
    print(f"  ✓ 크림 시세 수집: {kream_ok}/{len(matched)}건, "
          f"수익 기회: {len(opportunities)}건, {phase5_dur:.1f}초")

    # =================================================================
    # 결과 저장 + 요약 출력
    # =================================================================
    total = time.perf_counter() - t0
    results["phase_timings"]["total"] = round(total, 2)
    results["finished_at"] = datetime.now().isoformat()

    out_dir = Path("data/pilot_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"pilot_musinsa_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    print("\n" + "=" * 64)
    print(" 📊 파일럿 결과 요약 — 푸시 방식 가설 검증")
    print("=" * 64)
    print(f" 카테고리: {CATEGORY_SHOES} (신발) × {MAX_PAGES} 페이지")
    print(f" 크림 DB 인덱스: {results['counts']['kream_db_indexed']:,}건")
    print()
    print(" 파이프라인 통과량:")
    print(f"   덤프 총량:        {results['counts']['listing_dumped']:>6}건")
    print(f"   브랜드 필터:      {results['counts']['brand_filtered']:>6}건")
    print(f"   상세 수집:        {results['counts']['details_fetched']:>6}건")
    print(f"   모델번호 확보:    {results['counts']['details_with_model']:>6}건")
    print(f"   크림 DB 매칭:     {results['counts']['matched_to_kream']:>6}건")
    print(f"   크림 시세 수집:   {results['counts'].get('kream_realtime_ok', 0):>6}건")
    print(f"   수익 기회:        {results['counts']['profitable_opportunities']:>6}건")
    print()
    print(" 단계별 소요:")
    for phase, sec in results["phase_timings"].items():
        print(f"   {phase:22s} {sec:>8.2f}s")
    print()
    if opportunities:
        print(f" 🔥 수익 기회 TOP 5:")
        for i, opp in enumerate(opportunities[:5], 1):
            print(f"   {i}. [{opp['brand']}] {opp['model_number']} {opp['size']}")
            print(f"      무신사 {opp['retail_price']:,}원 → 크림 {opp['kream_sell_price']:,}원"
                  f" / 순수익 {opp['net_profit']:,}원 (ROI {opp['roi']}%)")
    else:
        print(" (수익 임계값 충족 없음)")

    if results["errors"]:
        print(f"\n ⚠ 에러 {len(results['errors'])}건 (첫 5개):")
        for err in results["errors"][:5]:
            print(f"   - {err}")

    print(f"\n 💾 상세 결과: {out_file}")
    print("=" * 64)

    await _cleanup()


async def _cleanup() -> None:
    try:
        await musinsa_crawler.close()
    except Exception:
        pass
    try:
        await kream_crawler.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
