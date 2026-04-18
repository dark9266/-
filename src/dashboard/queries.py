"""대시보드 읽기 전용 SQL 헬퍼."""

from __future__ import annotations

import json
import sqlite3  # noqa: F401 — OperationalError 예외 처리용
from typing import Any

from src.config import settings
from src.core.db import sync_connect

# 크림 일일 호출 캡 — src.core.kream_budget 의 BUDGET 과 동일 기본값.
# 여기서 import 하지 않는 이유: 대시보드는 완전히 읽기 전용 의존성만 유지하고,
# core 모듈 import 로 인한 사이드이펙트 (테이블 생성 등) 를 피하기 위함.
KREAM_DAILY_CAP_DEFAULT = 10000


def _conn():
    """읽기 전용 연결 컨텍스트 — PRAGMA 4종 helper(src/core/db.py) 경유."""
    return sync_connect(settings.db_path, read_only=True)


def recent_alerts(hours: int = 24, limit: int = 100) -> list[dict[str, Any]]:
    sql = """
        SELECT ah.created_at, ah.alert_type, ah.best_profit, ah.signal,
               ah.source, ah.roi, ah.direction, ah.retail_price,
               ah.kream_sell_price, ah.size, ah.kream_product_id,
               kp.brand, kp.name, kp.model_number
        FROM alert_history ah
        LEFT JOIN kream_products kp ON ah.kream_product_id = kp.product_id
        WHERE ah.created_at >= datetime('now', ?)
        ORDER BY ah.created_at DESC
        LIMIT ?
    """
    with _conn() as c:
        rows = c.execute(sql, (f"-{hours} hours", limit)).fetchall()
    return [dict(r) for r in rows]


def alert_stats_24h() -> dict[str, Any]:
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM alert_history WHERE created_at >= datetime('now','-24 hours')"
        ).fetchone()[0]
        by_signal = c.execute(
            """SELECT signal, COUNT(*) as cnt
               FROM alert_history WHERE created_at >= datetime('now','-24 hours')
               GROUP BY signal"""
        ).fetchall()
        max_profit = c.execute(
            """SELECT MAX(best_profit) FROM alert_history
               WHERE created_at >= datetime('now','-24 hours')"""
        ).fetchone()[0] or 0
    return {
        "total": total,
        "max_profit": max_profit,
        "by_signal": {r["signal"] or "-": r["cnt"] for r in by_signal},
    }


def source_search_counts(hours: int = 1) -> dict[str, dict[str, Any]]:
    """retail_products 기반 최근 검색 카운트."""
    sql = """
        SELECT source, COUNT(*) as cnt, MAX(fetched_at) as last_at
        FROM retail_products
        WHERE fetched_at >= datetime('now', ?)
        GROUP BY source
    """
    with _conn() as c:
        try:
            rows = c.execute(sql, (f"-{hours} hours",)).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {r["source"]: {"count": r["cnt"], "last_at": r["last_at"]} for r in rows}


def source_coverage() -> list[dict[str, Any]]:
    sql = """
        SELECT source, COUNT(DISTINCT model_number) as matched
        FROM retail_products
        WHERE model_number IS NOT NULL AND model_number != ''
        GROUP BY source
        ORDER BY matched DESC
    """
    with _conn() as c:
        try:
            total_kream = c.execute(
                "SELECT COUNT(*) FROM kream_products WHERE model_number != ''"
            ).fetchone()[0]
            rows = c.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []
    out = []
    for r in rows:
        matched = r["matched"]
        pct = round(matched * 100.0 / total_kream, 2) if total_kream else 0
        out.append({"source": r["source"], "matched": matched,
                    "total_kream": total_kream, "coverage_pct": pct})
    return out


def kream_brand_distribution(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            """SELECT brand, COUNT(*) as cnt
               FROM kream_products WHERE brand != ''
               GROUP BY brand ORDER BY cnt DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def kream_totals() -> dict[str, int]:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM kream_products").fetchone()[0]
        with_model = c.execute(
            "SELECT COUNT(*) FROM kream_products WHERE model_number != ''"
        ).fetchone()[0]
    return {"total": total, "with_model": with_model}


def daily_profit_series(days: int = 30) -> list[dict[str, Any]]:
    sql = """
        SELECT date(created_at) as day,
               COUNT(*) as cnt,
               COALESCE(AVG(best_profit), 0) as avg_profit,
               COALESCE(MAX(best_profit), 0) as max_profit
        FROM alert_history
        WHERE created_at >= datetime('now', ?)
        GROUP BY day ORDER BY day
    """
    with _conn() as c:
        rows = c.execute(sql, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def signal_distribution(days: int = 7) -> list[dict[str, Any]]:
    sql = """
        SELECT COALESCE(NULLIF(signal,''), '-') as signal, COUNT(*) as cnt
        FROM alert_history
        WHERE created_at >= datetime('now', ?)
        GROUP BY signal ORDER BY cnt DESC
    """
    with _conn() as c:
        rows = c.execute(sql, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Phase 3 runtime-sentinel 헬스핀 — event_checkpoint / kream_api_calls /
# alert_sent 읽기 전용 집계
# --------------------------------------------------------------------------


def adapter_health_24h() -> list[dict[str, Any]]:
    """어댑터(소싱처)별 최근 24h 헬스핀 — `event_checkpoint` 기반.

    `CatalogDumped` 이벤트의 payload(JSON) 에서 source/dumped/matched 를 뽑아
    소싱처별로 합산한다. status(pending/deferred/consumed/failed) 도 함께 집계.

    반환 필드:
        source, total, consumed, deferred, failed, pending,
        success_rate, last_at (ISO), dumped_24h, matched_24h
    """
    sql = """
        SELECT event_type, payload, created_at, status
        FROM event_checkpoint
        WHERE event_type = 'CatalogDumped'
          AND created_at >= (strftime('%s','now') - 86400)
    """
    agg: dict[str, dict[str, Any]] = {}
    with _conn() as c:
        try:
            rows = c.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        source = payload.get("source") or payload.get("adapter") or "-"
        bucket = agg.setdefault(
            source,
            {
                "source": source,
                "total": 0,
                "consumed": 0,
                "deferred": 0,
                "failed": 0,
                "pending": 0,
                "dumped_24h": 0,
                "matched_24h": 0,
                "last_at": 0.0,
            },
        )
        bucket["total"] += 1
        status = r["status"] or "pending"
        if status in bucket:
            bucket[status] += 1
        bucket["dumped_24h"] += int(payload.get("dumped", 0) or 0)
        bucket["matched_24h"] += int(payload.get("matched", 0) or 0)
        created = float(r["created_at"] or 0)
        if created > bucket["last_at"]:
            bucket["last_at"] = created

    out: list[dict[str, Any]] = []
    for bucket in agg.values():
        total = bucket["total"]
        consumed = bucket["consumed"]
        bucket["success_rate"] = (
            round(consumed * 100.0 / total, 1) if total else 0.0
        )
        bucket["last_at_iso"] = (
            _epoch_to_iso(bucket["last_at"]) if bucket["last_at"] else None
        )
        out.append(bucket)
    out.sort(key=lambda b: (-b["total"], b["source"]))
    return out


def kream_budget_usage(cap: int = KREAM_DAILY_CAP_DEFAULT) -> dict[str, Any]:
    """크림 API 일일 호출 예산 사용량 — 최근 24h.

    `kream_api_calls` 테이블 기반. 테이블이 없으면 used=0 반환.
    """
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM kream_api_calls "
                "WHERE ts >= datetime('now','-1 day')"
            ).fetchone()
            used = int(row[0]) if row else 0
        except sqlite3.OperationalError:
            used = 0

        purposes: list[dict[str, Any]] = []
        try:
            prows = c.execute(
                "SELECT COALESCE(NULLIF(purpose,''),'-') as purpose, "
                "       COUNT(*) as cnt "
                "FROM kream_api_calls "
                "WHERE ts >= datetime('now','-1 day') "
                "GROUP BY purpose ORDER BY cnt DESC"
            ).fetchall()
            purposes = [dict(r) for r in prows]
        except sqlite3.OperationalError:
            purposes = []

    cap = max(1, int(cap))
    ratio = round(used / cap, 3) if cap else 0.0
    return {
        "used": used,
        "cap": cap,
        "remaining": max(0, cap - used),
        "ratio": ratio,
        "pct": round(ratio * 100.0, 1),
        "purposes": purposes,
    }


def pipeline_counters_1h() -> dict[str, Any]:
    """이벤트 파이프라인 카운터 — 최근 1h status 별 count + event_type 별 count."""
    by_status: dict[str, int] = {
        "pending": 0,
        "deferred": 0,
        "consumed": 0,
        "failed": 0,
    }
    by_type: list[dict[str, Any]] = []
    with _conn() as c:
        try:
            srows = c.execute(
                "SELECT status, COUNT(*) as cnt "
                "FROM event_checkpoint "
                "WHERE created_at >= (strftime('%s','now') - 3600) "
                "GROUP BY status"
            ).fetchall()
            for r in srows:
                by_status[r["status"]] = int(r["cnt"])
            trows = c.execute(
                "SELECT event_type, status, COUNT(*) as cnt "
                "FROM event_checkpoint "
                "WHERE created_at >= (strftime('%s','now') - 3600) "
                "GROUP BY event_type, status "
                "ORDER BY event_type"
            ).fetchall()
            by_type = [dict(r) for r in trows]
        except sqlite3.OperationalError:
            pass

    total = sum(by_status.values())
    return {"by_status": by_status, "by_type": by_type, "total": total}


def recent_v3_alerts(limit: int = 10) -> list[dict[str, Any]]:
    """v3 경로 알림 피드 — `alert_sent` 최근 N건 (fired_at 내림차순)."""
    sql = """
        SELECT asent.id, asent.checkpoint_id, asent.kream_product_id,
               asent.signal, asent.fired_at,
               kp.brand, kp.name, kp.model_number
        FROM alert_sent asent
        LEFT JOIN kream_products kp
               ON CAST(asent.kream_product_id AS TEXT) = CAST(kp.product_id AS TEXT)
        ORDER BY asent.fired_at DESC
        LIMIT ?
    """
    with _conn() as c:
        try:
            rows = c.execute(sql, (limit,)).fetchall()
        except sqlite3.OperationalError:
            return []
    out = []
    for r in rows:
        d = dict(r)
        d["fired_at_iso"] = _epoch_to_iso(d.get("fired_at"))
        out.append(d)
    return out


def _epoch_to_iso(value: Any) -> str | None:
    """unix epoch(float) → ISO 문자열. None/0 은 None 반환."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(v).isoformat(timespec="seconds")
