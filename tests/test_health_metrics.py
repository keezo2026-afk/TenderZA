"""Health classification fixtures (Blueprint §15, §16).

Fixed clocks, no DB — these pin the decision table that decides whether a
human gets woken up. Getting this wrong in either direction is expensive:
false pages train operators to ignore the pager; missed pages mean a dark
source nobody notices for weeks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tenderza.health.metrics import (
    TIER_SLA_MINUTES,
    classify_source,
    freshness_sla_minutes,
    mttd_minutes,
    mttr_minutes,
    severity_rank,
    should_page,
    summarise_verdicts,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def row(**kw):
    base = {
        "id": "src-1",
        "name": "Test Source",
        "status": "ACTIVE",
        "triage_tier": 2,
        "frequency_min": 90,
        "last_success": NOW - timedelta(minutes=30),
        "consecutive_failures": 0,
        "breakage_started_at": None,
        "last_error": None,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# SLA table (§16: T1 30 min · T2 2 h · T3 6 h · T4 24 h)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier,expected", [(1, 30), (2, 120), (3, 360), (4, 1440)])
def test_tier_sla_matches_blueprint(tier, expected):
    assert freshness_sla_minutes(tier) == expected
    assert TIER_SLA_MINUTES[tier] == expected


def test_unknown_tier_falls_back_to_daily():
    assert freshness_sla_minutes(None) == 1440
    assert freshness_sla_minutes(99) == 1440


# --------------------------------------------------------------------------
# Happy path and staleness
# --------------------------------------------------------------------------

def test_recent_success_is_ok_and_does_not_page():
    v = classify_source(row(), now=NOW)
    assert v.state == "OK"
    assert v.page is False
    assert v.sla_breached is False


def test_stale_tier1_pages():
    # T1 SLA is 30 min, frequency 15 -> stale threshold 30 min.
    v = classify_source(
        row(triage_tier=1, frequency_min=15,
            last_success=NOW - timedelta(hours=3)),
        now=NOW,
    )
    assert v.state == "STALE"
    assert v.sla_breached is True
    assert v.page is True
    assert "SLA 30 min" in v.reason


def test_stale_tier4_does_not_page():
    """Low-tier staleness is a dashboard item, not a 2am phone call."""
    v = classify_source(
        row(triage_tier=4, frequency_min=1440,
            last_success=NOW - timedelta(days=5)),
        now=NOW,
    )
    assert v.state == "STALE"
    assert v.page is False


def test_grace_windows_prevent_false_staleness():
    """A source polled hourly cannot be fresher than an hour: one missed
    window must not trip the alarm."""
    v = classify_source(
        row(triage_tier=1, frequency_min=60,
            last_success=NOW - timedelta(minutes=90)),
        now=NOW,
    )
    # 90 min > 30 min SLA, but < 2 polling windows (120 min) -> still OK.
    assert v.state == "OK"


# --------------------------------------------------------------------------
# Failure states
# --------------------------------------------------------------------------

def test_degraded_within_mttd_target_does_not_page():
    v = classify_source(
        row(status="DEGRADED", consecutive_failures=1,
            breakage_started_at=NOW - timedelta(minutes=30),
            last_error="HTTP 500"),
        now=NOW,
    )
    assert v.state == "DEGRADED"
    assert v.page is False
    assert "retrying with backoff" in v.reason


def test_degraded_beyond_mttd_target_pages():
    """§16: MTTD < 4 h for T1/T2 — past that, a human must know."""
    v = classify_source(
        row(status="DEGRADED", consecutive_failures=3,
            breakage_started_at=NOW - timedelta(hours=6),
            last_error="HTTP 503"),
        now=NOW,
    )
    assert v.state == "DEGRADED"
    assert v.page is True
    assert v.mttd_target_minutes == 240
    assert v.breakage_minutes == pytest.approx(360)


def test_terminal_failure_always_pages():
    v = classify_source(
        row(status="FAILED", consecutive_failures=5,
            breakage_started_at=NOW - timedelta(hours=2),
            last_error="connection reset"),
        now=NOW,
    )
    assert v.state == "FAILED"
    assert v.page is True
    assert "connection reset" in v.reason


def test_attempt_cap_reached_is_failed_even_if_registry_lags():
    v = classify_source(row(status="ACTIVE", consecutive_failures=5), now=NOW)
    assert v.state == "FAILED"


# --------------------------------------------------------------------------
# Expected non-crawling states must never page (§3.6, §5.2)
# --------------------------------------------------------------------------

def test_publish_nothing_is_not_a_failure():
    v = classify_source(
        row(status="PUBLISH_NOTHING", last_success=None,
            triage_tier=4),
        now=NOW,
    )
    assert v.state == "PUBLISH_NOTHING"
    assert v.page is False
    assert "§3.6" in v.reason


def test_discovery_source_is_not_a_failure():
    v = classify_source(row(status="DISCOVERY", last_success=None), now=NOW)
    assert v.state == "DISCOVERY"
    assert v.page is False


def test_inactive_source_never_pages():
    v = classify_source(row(status="INACTIVE", last_success=None), now=NOW)
    assert v.page is False


def test_never_run_tier1_pages_but_tier4_does_not():
    hot = classify_source(row(triage_tier=1, last_success=None), now=NOW)
    cold = classify_source(row(triage_tier=4, last_success=None), now=NOW)
    assert hot.state == cold.state == "NEVER_RUN"
    assert hot.page is True
    assert cold.page is False


# --------------------------------------------------------------------------
# MTTD / MTTR helpers (§16)
# --------------------------------------------------------------------------

def test_mttd_and_mttr_arithmetic():
    started = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
    detected = datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc)
    recovered = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)
    assert mttd_minutes(started, detected) == 90
    assert mttr_minutes(started, recovered) == 720


def test_mttd_none_when_no_breakage():
    assert mttd_minutes(None, NOW) is None
    assert mttr_minutes(NOW, None) is None


# --------------------------------------------------------------------------
# Aggregation / paging order
# --------------------------------------------------------------------------

def test_severity_orders_worst_first():
    assert severity_rank("FAILED") < severity_rank("DEGRADED") < severity_rank("STALE")
    assert severity_rank("OK") > severity_rank("STALE")


def test_should_page_sorts_failed_before_degraded():
    verdicts = [
        classify_source(row(id="a", name="Degraded", status="DEGRADED",
                            consecutive_failures=2,
                            breakage_started_at=NOW - timedelta(hours=9)), now=NOW),
        classify_source(row(id="b", name="Dark", status="FAILED",
                            consecutive_failures=5), now=NOW),
        classify_source(row(id="c", name="Fine"), now=NOW),
    ]
    paging = should_page(verdicts)
    assert [v.name for v in paging] == ["Dark", "Degraded"]


def test_summary_excludes_publish_nothing_from_coverage_kpi():
    """Parking dead municipalities must not flatter the coverage number (§16)."""
    verdicts = [
        classify_source(row(id=f"ok{i}", name=f"OK {i}"), now=NOW) for i in range(19)
    ]
    verdicts.append(
        classify_source(row(id="dead", name="Dead Muni", status="PUBLISH_NOTHING",
                            last_success=None), now=NOW)
    )
    verdicts.append(
        classify_source(row(id="bad", name="Broken", status="FAILED",
                            consecutive_failures=5), now=NOW)
    )
    s = summarise_verdicts(verdicts)
    assert s["sources_total"] == 21
    assert s["crawlable"] == 20          # PUBLISH_NOTHING excluded
    assert s["healthy"] == 19
    assert s["healthy_pct"] == 95.0
    assert s["sla_met"] is True
    assert s["paging"] == 1
    assert s["worst"][0]["name"] == "Broken"


def test_summary_flags_below_target():
    verdicts = [classify_source(row(id="ok", name="OK"), now=NOW)]
    verdicts += [
        classify_source(row(id=f"b{i}", name=f"Broken {i}", status="FAILED",
                            consecutive_failures=5), now=NOW)
        for i in range(3)
    ]
    s = summarise_verdicts(verdicts)
    assert s["healthy_pct"] == 25.0
    assert s["sla_met"] is False


def test_summary_handles_empty_registry():
    s = summarise_verdicts([])
    assert s["crawlable"] == 0
    assert s["healthy_pct"] is None
    assert s["sla_met"] is False
