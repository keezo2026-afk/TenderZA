"""Deduplication, fingerprinting & authority merge (Blueprint §7, §8).

* fingerprint(): stable hash of (buyer key + normalized tender number +
  normalized title + closing date) — the primary duplicate key.
* titles_similar(): cheap token-overlap fuzzy match for differently-
  formatted duplicates, meant to run within (buyer, province, closing
  month) blocks (§7).
* merge_tenders(): authority resolution — the higher-authority source
  wins each field; conflicts are logged into the merged record's
  ``conflicts`` list for admin surfacing (§8).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from tenderza.pipeline.entity_resolution import normalize_org_name
from tenderza.pipeline.normalizer import CanonicalTender

_WORD = re.compile(r"[a-z0-9]+")

# Fields merged by authority (§8). Listed explicitly: merging is a
# deliberate act, not a loop over __dict__.
_MERGE_FIELDS = [
    "title",
    "tender_number",
    "normalized_tender_number",
    "buyer_name",
    "buyer_org_id",
    "description",
    "province",
    "published_at",
    "closing_at",
    "briefing_at",
    "compulsory_briefing",
    "value_estimated",
]


def fingerprint(tender: CanonicalTender) -> str:
    """Stable duplicate key (§7). Empty components stay empty — a missing
    tender number must not accidentally collide across buyers."""
    buyer_key = tender.buyer_org_id or normalize_org_name(tender.buyer_name)
    title_key = "".join(_WORD.findall(tender.title.casefold()))[:120]
    closing_key = (
        tender.closing_at.date().isoformat() if tender.closing_at else ""
    )
    basis = "|".join(
        [buyer_key, tender.normalized_tender_number, title_key, closing_key]
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def natural_key(tender: CanonicalTender) -> str | None:
    """Closing-date-independent identity: (buyer key, normalized number).

    The fingerprint includes the closing date, so a closing-date EXTENSION
    (§9's most important change type!) changes the fingerprint. Persistence
    must therefore look up by natural key first, falling back to fingerprint
    for records without a tender number. Returns None when either component
    is missing — a bare title is not a safe identity.
    """
    buyer_key = tender.buyer_org_id or normalize_org_name(tender.buyer_name)
    if not buyer_key or not tender.normalized_tender_number:
        return None
    return f"{buyer_key}|{tender.normalized_tender_number}"


def titles_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Jaccard token overlap — cheap fuzzy duplicate signal (§7)."""
    ta = set(_WORD.findall(a.casefold()))
    tb = set(_WORD.findall(b.casefold()))
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def are_duplicates(a: CanonicalTender, b: CanonicalTender) -> bool:
    """Two-stage duplicate check: exact fingerprint, then blocked fuzzy."""
    if fingerprint(a) == fingerprint(b):
        return True
    # Same normalized tender number + same buyer key => duplicate even if
    # titles were formatted differently.
    if (
        a.normalized_tender_number
        and a.normalized_tender_number == b.normalized_tender_number
        and normalize_org_name(a.buyer_name) == normalize_org_name(b.buyer_name)
    ):
        return True
    # Fuzzy path (§7): same buyer + same closing date + similar title.
    if (
        normalize_org_name(a.buyer_name) == normalize_org_name(b.buyer_name)
        and a.closing_at
        and b.closing_at
        and a.closing_at.date() == b.closing_at.date()
        and titles_similar(a.title, b.title)
    ):
        return True
    return False


def merge_tenders(a: CanonicalTender, b: CanonicalTender) -> CanonicalTender:
    """Merge two records of the SAME real tender by source authority (§8).

    Returns the winner (higher authority) mutated with:
    * any fields it was missing filled from the loser;
    * merged source_urls / documents / field_provenance;
    * ``raw['conflicts']`` listing fields where both sides had values that
      disagree — the admin surfacing hook of §8.
    """
    winner, loser = (a, b) if a.authority_score >= b.authority_score else (b, a)
    conflicts: list[dict[str, Any]] = list(winner.raw.get("conflicts", []))

    for field_name in _MERGE_FIELDS:
        w_val = getattr(winner, field_name)
        l_val = getattr(loser, field_name)
        if w_val is None or w_val == "" or w_val == []:
            if l_val not in (None, "", []):
                setattr(winner, field_name, l_val)
                prov = loser.field_provenance.get(field_name)
                if prov:
                    winner.field_provenance[field_name] = prov
        elif l_val not in (None, "", []) and w_val != l_val:
            conflicts.append(
                {
                    "field": field_name,
                    "winner_source": winner.source_id,
                    "winner_value": str(w_val),
                    "loser_source": loser.source_id,
                    "loser_value": str(l_val),
                }
            )

    # Union of attribution and documents (§7: canonical record tracks all
    # source URLs).
    for url in loser.source_urls:
        if url not in winner.source_urls:
            winner.source_urls.append(url)
    seen_docs = {d["url"] for d in winner.documents}
    for doc in loser.documents:
        if doc["url"] not in seen_docs:
            winner.documents.append(doc)

    # Keep the loser's provenance for fields the winner lacks provenance on.
    for key, prov in loser.field_provenance.items():
        winner.field_provenance.setdefault(key, prov)

    # Review items travel with the canonical record.
    winner.review_items.extend(loser.review_items)

    if conflicts:
        winner.raw["conflicts"] = conflicts
    return winner
