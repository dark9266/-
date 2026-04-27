#!/usr/bin/env python3
"""최근 알림 자세히 list — 크림 URL + 소싱처 URL + 거래량 + ROI.

Usage:
    python scripts/recent_alerts.py                  # 최근 15건
    python scripts/recent_alerts.py --limit 30
    python scripts/recent_alerts.py --signal WATCH   # 관망만
    python scripts/recent_alerts.py --min-vol 3      # 거래량 ≥ 3 만
    python scripts/recent_alerts.py --sort vol       # 거래량 내림차순
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALERT_LOG = ROOT / "logs" / "v3_alerts.jsonl"
DB = ROOT / "data" / "kream_bot.db"

SIG_KR = {
    "STRONG_BUY": "강력매수",
    "BUY": "매수    ",
    "WATCH": "관망    ",
    "NOT_RECOMMENDED": "비추    ",
}


def load_alerts(limit: int) -> list[dict]:
    if not ALERT_LOG.exists():
        return []
    with ALERT_LOG.open() as f:
        lines = f.readlines()
    return [json.loads(line) for line in lines[-limit * 3:]]  # 중복 가능 → 여유 ×3


def get_kream_name(c: sqlite3.Cursor, pid: int) -> str:
    c.execute("SELECT name FROM kream_products WHERE product_id=?", (str(pid),))
    row = c.fetchone()
    return (row[0] or "")[:50] if row else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument(
        "--signal",
        choices=["STRONG_BUY", "BUY", "WATCH", "NOT_RECOMMENDED", "ALL"],
        default="ALL",
    )
    ap.add_argument("--min-vol", type=int, default=0)
    ap.add_argument("--sort", choices=["recent", "vol", "profit", "roi"], default="recent")
    ap.add_argument("--unique", action="store_true", help="동일 모델 중복 제거")
    args = ap.parse_args()

    rows = load_alerts(args.limit)
    if args.signal != "ALL":
        rows = [r for r in rows if r["signal"] == args.signal]
    if args.min_vol > 0:
        rows = [r for r in rows if r.get("volume_7d", 0) >= args.min_vol]

    if args.unique:
        seen: set = set()
        deduped = []
        for r in reversed(rows):
            key = (r["source"], r["model_no"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        rows = list(reversed(deduped))

    if args.sort == "vol":
        rows.sort(key=lambda r: r.get("volume_7d", 0), reverse=True)
    elif args.sort == "profit":
        rows.sort(key=lambda r: r.get("net_profit", 0), reverse=True)
    elif args.sort == "roi":
        rows.sort(key=lambda r: r.get("roi", 0), reverse=True)

    rows = rows[: args.limit]

    if not rows:
        print("(매칭 알림 없음)")
        return 0

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    print(f"{'시그널':9s} {'모델':20s} {'source':10s} {'순수익':>9s} {'ROI':>6s} {'vol':>4s}  상품명")
    print("=" * 130)
    for r in rows:
        sig = SIG_KR.get(r["signal"], r["signal"])
        pid = r.get("kream_product_id")
        name = get_kream_name(c, pid) if pid else ""
        catch = " 💡" if r.get("catch_applied") else ""
        print(
            f"{sig:9s} {r['model_no']:20s} {r['source']:10s} "
            f"₩{r['net_profit']:>8,} {r['roi']:>5.1f}% {r['volume_7d']:>4} "
            f"{name}{catch}"
        )
        print(f"          🔗 크림: https://kream.co.kr/products/{pid}")
        print(f"          🛒 {r['source']}: {r['url']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
