"""대시보드 읽기 전용 SQL 헬퍼."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.config import settings


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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
