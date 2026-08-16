"""Source-health metrics — pure functions (Blueprint §15, §16).

Everything here is deterministic and unit-tested with fixed clocks: no DB,
no network. ``tenderza.health.queries`` supplies the rows, this module
decides what they *mean*, and only then does anything page a human.

The blueprint is blunt about it (§15): "A health dashboard nobody gets
alerted on is decoration." So classification is designed around one
question — *does a human need to act, and how fast?*

Definitions used here
---------------------
freshness SLA (§16)   per-tier ceiling on publish-to-ingest delay:
                      T1 30 min · T2 2 h · T3 6 h · T4 24 h.
staleness             time since the source last produced a SUCCESSFUL
                      crawl. Compared against the tier SLA with a grace
                      factor, because a source polled hourly cannot be
                      fresher than an hour.
MTTD (§16)            time from first failed run (breakage began) to the
                      moment the breakage was detected/acknowledged.
                      Target: < 4 h for T1/T2.
MTTR (§16)            time from breakage start to the first success that
                      cleared it. Target: < 24 h.

PUBLISH_NOTHING sources (§3.6/§5.2) are *not* failures — a 200 response
with no tenders is the expected state. They are reported separately so
they never pollute the failure signal or the coverage KPI.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# §16 freshness SLA per triage tier, in minutes.
TIER_SLA_MINUTES: dict[int, int] = {1: 30, 2: 120, 3: 360, 4: 1440}
DEFAULT_SLA_MINUTES = 1440

# §16 MTTD targets: detect breakage within 4 h on T1/T2, 24 h below that.
TIER_MTTD_TARGET_MINUTES: dict[int, int] = {1: 240, 2: 240, 3: 1440, 4: 1440}
MTTR_TARGET_MINUTES = 1440  # < 24 h, all tiers (§16)

# A source polled every N minutes cannot be fresher than N minutes; allow
# two missed windows before calling it stale, so ordinary jitter and a
# single transient retry don't wake anybody up.
STALENESS_GRACE_WINDOWS = 2

# Ordered worst-first: drives dashboard sort and pager severity.
SEVERITY_ORDER = (
    "FAILED",       # terminal: attempts exhausted, source is dark
    "DEGRADED",     # failing runs, still retrying
    "STALE",        # runs "succeed" (or never run) but data is past SLA
    "NEVER_RUN",    # registered, never crawled — Phase-0 backlog
    "DISCOVERY",    # awaiting fingerprinting (§5.2) — expected, not a fault
    "PUBLISH_NOTHING",  # reachable, publishes nothing (§3.6) — expected
    "OK",
)


def severity_rank(verdict_state: str) -> int:
    """Lower = worse. Unknown states sort just above OK."""
    try:
        return SEVERITY_ORDER.index(verdict_state)
    except ValueError:
        return len(SEVERITY_ORDER) - 1


def freshness_sla_minutes(tier: int | None) -> int:
    """Per-tier publish-to-ingest ceiling (§16)."""
    return TIER_SLA_MINUTES.get(tier or 0, DEFAULT_SLA_MINUTES)


def mttd_target_minutes(tier: int | None) -> int:
    return TIER_MTTD_TARGET_MINUTES.get(tier or 0, 1440)


def minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    """Whole-minute delta, or None when either endpoint is missing."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def mttd_minutes(breakage_started_at: datetime | None,
                 detected_at: datetime | None) -> float | None:
    """Mean-time-to-detect for one breakage episode (§16)."""
    return minutes_between(breakage_started_at, detected_at)


def mttr_minutes(breakage_started_at: datetime | None,
                 recovered_at: datetime | None) -> float | None:
    """Mean-time-to-repair for one breakage episode (§16)."""
    return minutes_between(breakage_started_at, recovered_at)


@dataclass
class HealthVerdict:
    """What the dashboard shows for one source, and whether it pages."""

    source_id: str
    name: str
    state: str                      # see SEVERITY_ORDER
    tier: int | None
    reason: str
    minutes_since_success: float | None = None
    sla_minutes: int = DEFAULT_SLA_MINUTES
    sla_breached: bool = False
    breakage_minutes: float | None = None   # open-episode MTTD-so-far
    mttd_target_minutes: int | None = None
    page: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["severity"] = severity_rank(self.state)
        return d


def classify_source(row: dict[str, Any], *, now: datetime) -> HealthVerdict:
    """Turn one joined source+health row into a verdict.

    Expected keys (all optional except id/name):
        status, triage_tier, frequency_min, last_success, last_checked,
        consecutive_failures, breakage_started_at, last_error,
        open_job_state, notices_last_run
    """
    tier = row.get("triage_tier")
    sla = freshness_sla_minutes(tier)
    freq = row.get("frequency_min") or sla
    status = (row.get("status") or "").upper()
    failures = int(row.get("consecutive_failures") or 0)
    last_success = row.get("last_success")
    since_success = minutes_between(last_success, now)
    breakage = minutes_between(row.get("breakage_started_at"), now)
    target = mttd_target_minutes(tier)

    # Stale = past the tier SLA *and* past two polling windows, so a
    # daily-tier source is never called stale after ten minutes.
    stale_threshold = max(sla, freq * STALENESS_GRACE_WINDOWS)
    stale = since_success is not None and since_success > stale_threshold

    def verdict(state: str, reason: str, page: bool) -> HealthVerdict:
        return HealthVerdict(
            source_id=str(row.get("id") or row.get("source_id") or ""),
            name=row.get("name") or "(unnamed source)",
            state=state,
            tier=tier,
            reason=reason,
            minutes_since_success=since_success,
            sla_minutes=sla,
            sla_breached=bool(stale),
            breakage_minutes=breakage,
            mttd_target_minutes=target,
            page=page,
            consecutive_failures=failures,
            last_error=row.get("last_error"),
            detail={
                "frequency_min": freq,
                "stale_threshold_min": stale_threshold,
                "registry_status": status or None,
                "open_job_state": row.get("open_job_state"),
                "notices_last_run": row.get("notices_last_run"),
            },
        )

    # Expected non-crawling states first — these must never page (§3.6).
    if status == "PUBLISH_NOTHING":
        return verdict(
            "PUBLISH_NOTHING",
            "Reachable but publishes nothing; monthly life-sign check only (§3.6).",
            page=False,
        )
    if status == "INACTIVE":
        return verdict("PUBLISH_NOTHING", "Source marked inactive in the registry.",
                       page=False)
    if status == "DISCOVERY":
        return verdict(
            "DISCOVERY",
            "Awaiting platform fingerprinting (§5.2) — run discover_sources.py.",
            page=False,
        )

    # Terminal failure: attempts exhausted, the source is dark.
    if status == "FAILED" or failures >= 5:
        return verdict(
            "FAILED",
            f"Crawl failed terminally after {failures} attempt(s): "
            f"{row.get('last_error') or 'unknown error'}",
            page=True,
        )

    # Degraded: failing but still retrying. Pages once the open breakage
    # episode exceeds the tier's MTTD target (§16).
    if status == "DEGRADED" or failures > 0:
        over = breakage is not None and breakage > target
        return verdict(
            "DEGRADED",
            f"{failures} consecutive failure(s)"
            + (f"; breakage open {breakage:.0f} min > {target} min MTTD target"
               if over else "; retrying with backoff"),
            page=over,
        )

    if last_success is None:
        return verdict(
            "NEVER_RUN",
            "Registered but never crawled successfully.",
            # Only page for the tiers we promise freshness on.
            page=(tier in (1, 2)),
        )

    if stale:
        return verdict(
            "STALE",
            f"No successful crawl for {since_success:.0f} min "
            f"(SLA {sla} min, tier {tier}).",
            page=(tier in (1, 2)),
        )

    return verdict("OK", "Crawling within SLA.", page=False)


def should_page(verdicts: Iterable[HealthVerdict]) -> list[HealthVerdict]:
    """Subset that must wake a human, worst first (§15)."""
    return sorted(
        (v for v in verdicts if v.page),
        key=lambda v: (severity_rank(v.state), -(v.breakage_minutes or 0)),
    )


def summarise_verdicts(verdicts: Iterable[HealthVerdict]) -> dict[str, Any]:
    """Dashboard headline numbers + the §16 coverage KPI.

    ``crawlable`` deliberately excludes PUBLISH_NOTHING/DISCOVERY sources:
    the coverage KPI measures sources we *expect* to yield tenders, so
    parking a dead municipality can never flatter the number.
    """
    verdicts = list(verdicts)
    by_state: dict[str, int] = {}
    for v in verdicts:
        by_state[v.state] = by_state.get(v.state, 0) + 1

    excluded = by_state.get("PUBLISH_NOTHING", 0) + by_state.get("DISCOVERY", 0)
    crawlable = len(verdicts) - excluded
    healthy = by_state.get("OK", 0)
    paging = [v for v in verdicts if v.page]

    return {
        "sources_total": len(verdicts),
        "crawlable": crawlable,
        "healthy": healthy,
        "healthy_pct": round(100.0 * healthy / crawlable, 1) if crawlable else None,
        "by_state": by_state,
        "paging": len(paging),
        "worst": [v.as_dict() for v in should_page(verdicts)[:10]],
        # §16 target: ≥95% of crawlable sources succeeding inside their tier SLA.
        "sla_target_pct": 95.0,
        "sla_met": (crawlable > 0 and (100.0 * healthy / crawlable) >= 95.0),
    }


def window_start(now: datetime, hours: int) -> datetime:
    return now - timedelta(hours=hours)
