"""Normalizer: RawTenderNotice -> CanonicalTender (Blueprint §5.4, §10).

Responsibilities:
* canonicalize the tender number (§7);
* attach per-field provenance + confidence (§10.2.5) — SOURCE for fields
  parsed from structured data, INFERRED for guesses (e.g. SCM-convention
  closing time);
* apply the SCM closing-time convention: a closing DATE with no time is
  stored as 11:00 SAST and flagged INFERRED (§10.2.1);
* route high-stakes low-confidence fields to the review queue (§6).

The normalizer is pure: no I/O, no DB. Persistence happens downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any

from tenderza.adapters.base import RawTenderNotice
from tenderza.normalize import normalize_tender_number

SAST = timezone(timedelta(hours=2))
SCM_DEFAULT_CLOSING = time(11, 0)  # 11:00 SAST convention (§10.2.1)

# Confidence thresholds (§6): below this, high-stakes fields go to review.
REVIEW_THRESHOLD = 0.80
HIGH_STAKES_FIELDS = {"closing_at", "value_estimated", "requirements"}


@dataclass
class Provenance:
    source: str          # SOURCE | DERIVED | INFERRED
    source_id: str
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "confidence": self.confidence,
        }


@dataclass
class CanonicalTender:
    """Normalized tender ready for dedupe/persistence (§10.1 shape)."""

    title: str
    source_id: str
    source_url: str
    authority_score: int = 50
    tender_number: str | None = None
    normalized_tender_number: str = ""
    buyer_name: str | None = None
    buyer_org_id: str | None = None          # filled by entity resolution
    description: str | None = None
    province: str | None = None
    published_at: datetime | None = None
    closing_at: datetime | None = None
    briefing_at: datetime | None = None
    compulsory_briefing: bool | None = None
    value_estimated: float | None = None
    currency: str = "ZAR"
    requirements: dict[str, Any] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    field_provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_items: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def provenance_for(self, field_name: str) -> dict[str, Any] | None:
        return self.field_provenance.get(field_name)

    def confidence_for(self, field_name: str) -> float:
        prov = self.field_provenance.get(field_name)
        return prov["confidence"] if prov else 0.0


def _ensure_tz(dt: datetime | None) -> datetime | None:
    """Never store naive datetimes (§10.2.1). Naive input is assumed SAST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SAST)
    return dt


def normalize_notice(
    notice: RawTenderNotice,
    *,
    authority_score: int = 50,
    structured: bool = True,
) -> CanonicalTender:
    """Normalize a raw notice into a CanonicalTender with provenance.

    ``structured`` — True when the notice came from structured data (API,
    feed fields); False when fields were extracted from free text/PDF, which
    lowers baseline confidence and routes high-stakes fields to review.
    """
    src = notice.source_id
    base_conf = 0.98 if structured else 0.70
    kind = "SOURCE"

    tender = CanonicalTender(
        title=notice.title.strip(),
        source_id=src,
        source_url=notice.source_url,
        authority_score=authority_score,
        tender_number=notice.tender_number,
        normalized_tender_number=normalize_tender_number(notice.tender_number),
        buyer_name=notice.buyer_name,
        description=notice.description,
        published_at=_ensure_tz(notice.published_at),
        briefing_at=_ensure_tz(notice.briefing_at),
        compulsory_briefing=notice.compulsory_briefing,
        documents=[{"url": d.url, "filename": d.filename} for d in notice.documents],
        source_urls=[notice.source_url],
        raw=notice.raw,
    )

    prov = tender.field_provenance
    if notice.title:
        prov["title"] = Provenance(kind, src, base_conf).as_dict()
    if notice.tender_number:
        prov["tender_number"] = Provenance(kind, src, base_conf).as_dict()
    if notice.buyer_name:
        prov["buyer_name"] = Provenance(kind, src, base_conf).as_dict()
    if notice.published_at:
        prov["published_at"] = Provenance(kind, src, base_conf).as_dict()
    if notice.compulsory_briefing is not None:
        prov["compulsory_briefing"] = Provenance(kind, src, base_conf).as_dict()
    if notice.briefing_at:
        prov["briefing_at"] = Provenance(kind, src, base_conf).as_dict()

    # Closing date + SCM convention (§10.2.1)
    closing = _ensure_tz(notice.closing_at)
    if closing is not None:
        if closing.timetz().replace(tzinfo=None) == time(0, 0):
            # Date-only closing: apply 11:00 SAST convention, flag INFERRED.
            closing = closing.astimezone(SAST).replace(
                hour=SCM_DEFAULT_CLOSING.hour, minute=SCM_DEFAULT_CLOSING.minute
            )
            prov["closing_at"] = Provenance("INFERRED", src, 0.75).as_dict()
        else:
            prov["closing_at"] = Provenance(kind, src, base_conf).as_dict()
        tender.closing_at = closing

    # Review-queue routing (§6): high-stakes fields below threshold.
    for field_name in HIGH_STAKES_FIELDS:
        p = prov.get(field_name)
        if p and p["confidence"] < REVIEW_THRESHOLD:
            tender.review_items.append(
                {"field": field_name, "confidence": p["confidence"]}
            )

    return tender
