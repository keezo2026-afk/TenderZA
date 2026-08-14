"""Tender status computation (Blueprint §10.3).

* CLOSING_SOON is COMPUTED: closing within 72h of a VERIFIED closing_at.
* Unverified closing date (confidence below threshold, or no date) =>
  UNKNOWN — an unverified date never silently drives alerts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tenderza.pipeline.normalizer import CanonicalTender

CLOSING_SOON_WINDOW = timedelta(hours=72)
VERIFIED_THRESHOLD = 0.80  # matches the review-queue threshold (§6)


def compute_status(
    tender: CanonicalTender,
    *,
    now: datetime | None = None,
    cancelled: bool = False,
    awarded: bool = False,
) -> str:
    """Return the tender_status value per §10.3."""
    if cancelled:
        return "CANCELLED"
    if awarded:
        return "AWARDED"

    now = now or datetime.now(timezone.utc)

    closing = tender.closing_at
    confidence = tender.confidence_for("closing_at")
    if closing is None or confidence < VERIFIED_THRESHOLD:
        # UI shows "verify at source" (§10.3); alerts never fire off this.
        return "UNKNOWN"

    if closing <= now:
        return "CLOSED"
    if closing - now <= CLOSING_SOON_WINDOW:
        return "CLOSING_SOON"
    return "OPEN"
