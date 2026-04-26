"""크림봇 운영자 대시보드 — FastAPI + Jinja2 + HTMX.

실행:
    PYTHONPATH=. uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000

5개 판:
    /          — 실시간 수익 알림 피드
    /sources   — 소싱처 16곳 헬스
    /matching  — 크림 DB 매칭 품질
    /profit    — 수익 집계 + 그래프
    /health    — Phase 3 runtime-sentinel 헬스핀 (어댑터·크림캡·파이프라인·v3 알림)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# 크롤러 모듈 import 시점에 registry.register() 자동 호출 — 소싱처 판 표시를 위해 프리로드
from src.crawlers import (  # noqa: F401, E402
    abcmart,
    adidas,
    arcteryx,
    eql,
    kasina,
    musinsa_httpx,
    nbkorea,
    nike,
    registry,
    salomon,
    tune,
    twentynine_cm,
    vans,
    wconcept,
)
# worksout 폐기 (project_worksout_platform_permanent_dropped.md, 2026-04-19)
from src.dashboard import queries

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _fmt_won(value: int | float | None) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}원"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


templates.env.filters["won"] = _fmt_won
templates.env.filters["pct"] = _fmt_pct
templates.env.filters["time"] = _fmt_time

app = FastAPI(title="크림봇 대시보드", docs_url=None, redoc_url=None)


def _nav_ctx(request: Request, active: str) -> dict:
    return {
        "request": request,
        "active": active,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    alerts = queries.recent_alerts(hours=24, limit=100)
    stats = queries.alert_stats_24h()
    ctx = _nav_ctx(request, "feed")
    ctx.update({"alerts": alerts, "stats": stats})
    return templates.TemplateResponse(request, "feed.html", ctx)


@app.get("/feed/rows", response_class=HTMLResponse)
async def feed_rows(request: Request):
    """HTMX 부분 갱신용 — 테이블 body만 반환."""
    alerts = queries.recent_alerts(hours=24, limit=100)
    return templates.TemplateResponse(
        request, "_feed_rows.html", {"alerts": alerts}
    )


@app.get("/sources", response_class=HTMLResponse)
async def sources(request: Request):
    status_map = registry.get_status()  # 키 → "✅/⏸️ label ..."
    searches = queries.source_search_counts(hours=1)
    rows = []
    for key, entry in registry.get_all().items():
        active = registry.is_active(key)
        disabled_until = entry.get("disabled_until")
        rows.append({
            "key": key,
            "label": entry.get("label", key),
            "active": active,
            "fail_count": entry.get("fail_count", 0),
            "disabled_until": (
                disabled_until.strftime("%H:%M") if disabled_until else None
            ),
            "searches_1h": searches.get(key, {}).get("count", 0),
            "last_at": searches.get(key, {}).get("last_at"),
        })
    rows.sort(key=lambda r: (not r["active"], -r["searches_1h"]))
    login_sources = {"musinsa": "세션 쿠키"}
    ctx = _nav_ctx(request, "sources")
    ctx.update({
        "rows": rows,
        "status_summary": status_map,
        "login_sources": login_sources,
    })
    return templates.TemplateResponse(request, "sources.html", ctx)


@app.get("/matching", response_class=HTMLResponse)
async def matching(request: Request):
    coverage = queries.source_coverage()
    brands = queries.kream_brand_distribution(limit=20)
    totals = queries.kream_totals()
    ctx = _nav_ctx(request, "matching")
    ctx.update({"coverage": coverage, "brands": brands, "totals": totals})
    return templates.TemplateResponse(request, "matching.html", ctx)


@app.get("/health", response_class=HTMLResponse)
async def health(request: Request):
    """Phase 3 runtime-sentinel 헬스핀.

    4개 위젯:
      1) 어댑터(소싱처)별 24h 덤프/매칭/실패율
      2) 크림 API 일일 캡 게이지
      3) 이벤트 파이프라인 카운터 (status × event_type)
      4) v3 알림 피드 최근 10건
    """
    adapters = queries.adapter_health_24h()
    budget = queries.kream_budget_usage()
    pipeline = queries.pipeline_counters_1h()
    v3_alerts = queries.recent_v3_alerts(limit=10)
    ctx = _nav_ctx(request, "health")
    ctx.update({
        "adapters": adapters,
        "budget": budget,
        "pipeline": pipeline,
        "v3_alerts": v3_alerts,
    })
    return templates.TemplateResponse(request, "health.html", ctx)


@app.get("/profit", response_class=HTMLResponse)
async def profit(request: Request):
    daily = queries.daily_profit_series(days=30)
    signal_dist = queries.signal_distribution(days=7)
    stats = queries.alert_stats_24h()
    ctx = _nav_ctx(request, "profit")
    ctx.update({
        "daily": daily,
        "signal_dist": signal_dist,
        "stats": stats,
        "daily_labels": [r["day"] for r in daily],
        "daily_counts": [r["cnt"] for r in daily],
        "daily_avg": [round(r["avg_profit"]) for r in daily],
        "signal_labels": [r["signal"] for r in signal_dist],
        "signal_counts": [r["cnt"] for r in signal_dist],
    })
    return templates.TemplateResponse(request, "profit.html", ctx)
