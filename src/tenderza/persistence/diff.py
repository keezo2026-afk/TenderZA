"""Change detection: field diffs for tender versioning (Blueprint §9).

Pure functions — no I/O. The store records a new tender_versions row when
diff_tender_fields() reports changes, and classify_change() labels the
version (EXTENDED / CANCELLED / ...) for change alerts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Fields tracked for versioning (§9). Documents are diffed separately by
# content hash.
TRACKED_FIELDS = [
    "title",
    "tender_number",
    "description",
    "buyer_name",
    "province",
    "status",
    "published_at",
    "closing_at",
    "briefing_at",
    "compulsory_briefing",
    "value_estimated",
    "currency",
]


def _plain(value: Any) -> Any:
    """JSON-safe representation for the changes blob."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def diff_tender_fields(
    old: dict[str, Any], new: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Compare two field dicts; return {field: {old, new}} for changes.

    A change is only recorded when the NEW side actually has a value —
    a source temporarily omitting a field must not erase history (§9's
    "recovery if a site reverts content" requirement is served by keeping
    the old value and logging nothing).
    """
    changes: dict[str, dict[str, Any]] = {}
    for field_name in TRACKED_FIELDS:
        old_val = old.get(field_name)
        new_val = new.get(field_name)
        if new_val is None:
            continue  # absence is not a change
        if old_val != new_val:
            changes[field_name] = {"old": _plain(old_val), "new": _plain(new_val)}
    return changes


def classify_change(changes: dict[str, dict[str, Any]]) -> str | None:
    """Label a version for change alerts (§9).

    Returns one of: EXTENDED, SHORTENED, CANCELLED, RE_ADVERTISED,
    DETAILS_CHANGED — or None when there is nothing to classify.
    """
    if not changes:
        return None

    status_change = changes.get("status")
    if status_change:
        new_status = status_change["new"]
        if new_status == "CANCELLED":
            return "CANCELLED"
        if new_status == "RE_ADVERTISED":
            return "RE_ADVERTISED"

    closing = changes.get("closing_at")
    if closing and closing["old"] is not None:
        # ISO strings compare chronologically when offsets match; be safe
        # and parse.
        old_dt = datetime.fromisoformat(closing["old"])
        new_dt = datetime.fromisoformat(closing["new"])
        if new_dt > old_dt:
            return "EXTENDED"
        if new_dt < old_dt:
            return "SHORTENED"

    return "DETAILS_CHANGED"
