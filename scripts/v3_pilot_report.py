"""v3 파일럿 관측 리포트 — 한 번에 상태 확인용.

사용:
    PYTHONPATH=. python scripts/v3_pilot_report.py
    PYTHONPATH=. python scripts/v3_pilot_report.py --hours 6
    PYTHONPATH=. python scripts/v3_pilot_report.py --json   # JSON 출력

집계 대상:
    1. kream_api_calls — 24h 호출량, 캡 사용율, 엔드포인트별 분포
    2. v3_alerts.jsonl — 알림 발생 시각/시그널/순수익/ROI 분포
    3. alert_sent — UNIQUE(checkpoint_id) dedup 행수 (v3 경로만)
    4. event_checkpoint — pending/consumed/deferred/failed 분포
    5. alert_followup — 체결 관측 진행 (Phase 4 진입 후 의미)

읽기 전용 — DB 변경 없음.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_DB = "data/kream_bot.db"
DEFAULT_JSONL = "logs/v3_alerts.jsonl"
DEFAULT_CAP = int(os.getenv("KREAM_DAILY_CAP", "10000"))


def _connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"❌ DB 없음: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ─── 섹션 1: 크림 호출량 + 캡 사용율 ──────────────────────


def kream_calls_summary(conn: sqlite3.Connection, hours: int) -> dict[str, Any]:
    if not _table_exists(conn, "kream_api_calls"):
        return {"available": False}

    since = f"-{hours} hours"
    rows = conn.execute(
        f"SELECT endpoint, status, purpose, latency_ms FROM kream_api_calls "
        f"WHERE ts >= datetime('now', '{since}')"
    ).fetchall()

    total = len(rows)
    by_status = Counter(r["status"] for r in rows)
    by_purpose = Counter(r["purpose"] or "—" for r in rows)
    by_endpoint = Counter(r["endpoint"] for r in rows)
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    p50 = sorted(latencies)[len(latencies) // 2] if latencies else None
    p95 = (
        sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 20 else None
    )

    cap_pct = round(total / DEFAULT_CAP * 100, 2) if DEFAULT_CAP else None
    return {
        "available": True,
        "window_hours": hours,
        "total_calls": total,
        "cap": DEFAULT_CAP,
        "cap_pct": cap_pct,
        "status_breakdown": dict(by_status),
        "top_purposes": dict(by_purpose.most_common(8)),
        "top_endpoints": dict(by_endpoint.most_common(8)),
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
    }


# ─── 섹션 2: v3 알림 (JSONL) ──────────────────────────────


def v3_alerts_summary(jsonl_path: str, hours: int) -> dict[str, Any]:
    path = Path(jsonl_path)
    if not path.exists():
        return {"available": False, "path": jsonl_path}

    cutoff = time.time() - hours * 3600
    recs: list[dict[str, Any]] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        ts = rec.get("fired_at") or rec.get("ts") or 0
        try:
            if float(ts) >= cutoff:
                recs.append(rec)
        except (TypeError, ValueError):
            recs.append(rec)

    if not recs:
        return {"available": True, "count": 0, "parse_errors": bad}

    signals = Counter(r.get("signal") or "—" for r in recs)
    sources = Counter(r.get("source") or "—" for r in recs)
    profits = [int(r.get("net_profit") or 0) for r in recs]
    rois = [float(r.get("roi") or 0) for r in recs]
    return {
        "available": True,
        "count": len(recs),
        "parse_errors": bad,
        "signals": dict(signals),
        "sources": dict(sources),
        "profit_min": min(profits) if profits else None,
        "profit_median": sorted(profits)[len(profits) // 2] if profits else None,
        "profit_max": max(profits) if profits else None,
        "roi_min": round(min(rois), 1) if rois else None,
        "roi_median": round(sorted(rois)[len(rois) // 2], 1) if rois else None,
        "roi_max": round(max(rois), 1) if rois else None,
    }


# ─── 섹션 3: alert_sent dedup (v3) ────────────────────────


def alert_sent_summary(conn: sqlite3.Connection, hours: int) -> dict[str, Any]:
    if not _table_exists(conn, "alert_sent"):
        return {"available": False}
    since = f"-{hours} hours"
    total = conn.execute(
        f"SELECT COUNT(*) FROM alert_sent WHERE fired_at >= "
        f"strftime('%s', 'now', '{since}')"
    ).fetchone()[0]
    by_signal = dict(
        conn.execute(
            f"SELECT signal, COUNT(*) FROM alert_sent "
            f"WHERE fired_at >= strftime('%s', 'now', '{since}') "
            f"GROUP BY signal ORDER BY 2 DESC"
        ).fetchall()
    )
    return {"available": True, "total": total, "by_signal": by_signal}


# ─── 섹션 4: 체크포인트 분포 ───────────────────────────────


def checkpoint_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "event_checkpoint"):
        return {"available": False}
    by_status = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM event_checkpoint GROUP BY status"
        ).fetchall()
    )
    by_event = dict(
        conn.execute(
            "SELECT event_type, COUNT(*) FROM event_checkpoint GROUP BY event_type"
        ).fetchall()
    )
    return {"available": True, "by_status": by_status, "by_event_type": by_event}


# ─── 섹션 5: alert_followup (Phase 4) ─────────────────────


def followup_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "alert_followup"):
        return {"available": False}
    total = conn.execute("SELECT COUNT(*) FROM alert_followup").fetchone()[0]
    sold = conn.execute(
        "SELECT COUNT(*) FROM alert_followup WHERE actual_sold = 1"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM alert_followup WHERE checked_at IS NULL"
    ).fetchone()[0]
    return {
        "available": True,
        "total": total,
        "sold": sold,
        "pending_check": pending,
        "hit_rate_pct": round(sold / total * 100, 2) if total else None,
    }


# ─── 텍스트 출력 ─────────────────────────────────────────


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"📊 v3 파일럿 리포트 — {report['generated_at']}")
    lines.append("=" * 60)

    k = report["kream_calls"]
    if not k.get("available"):
        lines.append("\n[크림 호출] kream_api_calls 테이블 없음")
    else:
        bar = "█" * int(min(k["cap_pct"] or 0, 100) / 5)
        lines.append(
            f"\n[크림 호출 {k['window_hours']}h] {k['total_calls']}/{k['cap']} "
            f"({k['cap_pct']}%) {bar}"
        )
        lines.append(f"  status: {k['status_breakdown']}")
        lines.append(f"  purpose top: {k['top_purposes']}")
        lines.append(
            f"  latency p50/p95: {k['latency_p50_ms']}/{k['latency_p95_ms']} ms"
        )

    a = report["v3_alerts"]
    if not a.get("available"):
        lines.append(f"\n[v3 알림] {a.get('path')} 파일 없음 — 미가동 추정")
    elif a["count"] == 0:
        lines.append(f"\n[v3 알림] 0건 (window {report['hours']}h)")
    else:
        lines.append(
            f"\n[v3 알림 {report['hours']}h] {a['count']}건 "
            f"(파싱 실패 {a['parse_errors']})"
        )
        lines.append(f"  signals: {a['signals']}")
        lines.append(f"  sources: {a['sources']}")
        lines.append(
            f"  profit min/med/max: {a['profit_min']}/{a['profit_median']}/{a['profit_max']}"
        )
        lines.append(
            f"  ROI    min/med/max: {a['roi_min']}/{a['roi_median']}/{a['roi_max']}"
        )

    s = report["alert_sent"]
    if not s.get("available"):
        lines.append("\n[alert_sent] 테이블 없음 — v3 첫 알림 전")
    else:
        lines.append(
            f"\n[alert_sent {report['hours']}h] {s['total']}건 — {s['by_signal']}"
        )

    c = report["checkpoints"]
    if not c.get("available"):
        lines.append("\n[체크포인트] event_checkpoint 테이블 없음")
    else:
        lines.append(f"\n[체크포인트] status={c['by_status']}")
        lines.append(f"            event_type={c['by_event_type']}")

    f = report["followup"]
    if not f.get("available"):
        lines.append("\n[follow-up] 테이블 없음")
    else:
        lines.append(
            f"\n[follow-up] 총 {f['total']} 체결 {f['sold']} "
            f"미체크 {f['pending_check']} 적중률 {f['hit_rate_pct']}%"
        )

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def build_report(db_path: str, jsonl_path: str, hours: int) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hours": hours,
            "db": db_path,
            "jsonl": jsonl_path,
            "kream_calls": kream_calls_summary(conn, hours),
            "v3_alerts": v3_alerts_summary(jsonl_path, hours),
            "alert_sent": alert_sent_summary(conn, hours),
            "checkpoints": checkpoint_summary(conn),
            "followup": followup_summary(conn),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="v3 파일럿 관측 리포트")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--jsonl", default=DEFAULT_JSONL)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    args = parser.parse_args()

    report = build_report(args.db, args.jsonl, args.hours)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
