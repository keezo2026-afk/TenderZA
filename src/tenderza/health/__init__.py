"""Source-health monitoring (Blueprint §15, §16).

Pure metric logic lives in ``metrics``; DB reads live in ``queries``.
The API router (``tenderza.api.health``) and the pager script
(``scripts/check_source_health.py``) both build on these.
"""

from tenderza.health.metrics import (
    HealthVerdict,
    classify_source,
    freshness_sla_minutes,
    minutes_between,
    mttd_minutes,
    mttr_minutes,
    severity_rank,
    should_page,
    summarise_verdicts,
)

__all__ = [
    "HealthVerdict",
    "classify_source",
    "freshness_sla_minutes",
    "minutes_between",
    "mttd_minutes",
    "mttr_minutes",
    "severity_rank",
    "should_page",
    "summarise_verdicts",
]
