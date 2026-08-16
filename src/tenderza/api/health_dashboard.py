"""Source-health dashboard endpoints (Blueprint §15, §16).

GET /ops/sources     per-source verdicts (worst first) + summary
GET /ops/overview    everything the dashboard page needs, in one call
GET /ops/crawl       24h crawl stats
GET /ops/freshness   per-tier freshness vs the §16 SLA table
GET /ops/adapters    per-adapter success trend (adapter-rot signal, §15)
GET /ops/failures    recent errors
GET /ops/alarms      ONLY the sources that must page a human (§15)

/ops/alarms returns 200 with an empty list when all is well and is safe
for an external monitor to poll — the pager script (scripts/check_source_health.py)
uses the same code path so the dashboard and the pager can never disagree.

Auth note: like /review, v1 is unauthenticated for the single-operator
admin; role-gate before public deployment.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from tenderza.health import should_page, summarise_verdicts
from tenderza.health.queries import (
    adapter_health,
    crawl_stats,
    freshness_by_tier,
    mttd_mttr_report,
    recent_failures,
    source_verdicts,
)

router = APIRouter(prefix="/ops", tags=["ops"])


def get_pool():
    from tenderza.api.app import get_pool as _gp
    return _gp()


@router.get("/sources")
def sources(state: str | None = Query(None, description="Filter by verdict state"),
            pool=Depends(get_pool)):
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        verdicts = source_verdicts(conn, now=now)
    rows = [v.as_dict() for v in verdicts]
    if state:
        rows = [r for r in rows if r["state"] == state.upper()]
    rows.sort(key=lambda r: (r["severity"], -(r["minutes_since_success"] or 0)))
    return {
        "generated_at": now.isoformat(),
        "summary": summarise_verdicts(verdicts),
        "sources": rows,
    }


@router.get("/alarms")
def alarms(pool=Depends(get_pool)):
    """Sources needing human action right now. Empty list = all good."""
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        verdicts = source_verdicts(conn, now=now)
    paging = should_page(verdicts)
    return {
        "generated_at": now.isoformat(),
        "count": len(paging),
        "alarms": [v.as_dict() for v in paging],
    }


@router.get("/crawl")
def crawl(hours: int = Query(24, ge=1, le=720), pool=Depends(get_pool)):
    with pool.connection() as conn:
        return crawl_stats(conn, hours=hours)


@router.get("/freshness")
def freshness(pool=Depends(get_pool)):
    with pool.connection() as conn:
        return {"tiers": freshness_by_tier(conn)}


@router.get("/adapters")
def adapters(days: int = Query(7, ge=1, le=90), pool=Depends(get_pool)):
    with pool.connection() as conn:
        return {"adapters": adapter_health(conn, days=days)}


@router.get("/failures")
def failures(limit: int = Query(20, ge=1, le=200), pool=Depends(get_pool)):
    with pool.connection() as conn:
        return {"failures": recent_failures(conn, limit=limit)}


@router.get("/overview")
def overview(pool=Depends(get_pool)):
    """Single call powering the dashboard page."""
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        verdicts = source_verdicts(conn, now=now)
        payload = {
            "generated_at": now.isoformat(),
            "summary": summarise_verdicts(verdicts),
            "crawl_24h": crawl_stats(conn, hours=24),
            "freshness": freshness_by_tier(conn),
            "adapters": adapter_health(conn, days=7),
            "failures": recent_failures(conn, limit=10),
            "reliability": mttd_mttr_report(conn, days=30),
        }
    rows = [v.as_dict() for v in verdicts]
    rows.sort(key=lambda r: (r["severity"], -(r["minutes_since_success"] or 0)))
    payload["sources"] = rows
    return payload
