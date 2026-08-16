"""Timezone doctrine for TenderZA (Blueprint §10.2.1).

One rule, applied everywhere: **a tender instant is stored as a tz-aware
`timestamptz`, never a naive datetime and never a bare date**. South Africa
runs on SAST (UTC+02:00) year-round — no DST, no historical transitions that
matter for procurement deadlines — so a fixed offset is correct and cheaper
than a tz database lookup.

Why this module exists
----------------------
Publishers are inconsistent about zones, and the failure mode is expensive:
a two-hour error on a closing date can make a user miss a bid. Rather than
sprinkling ``fromisoformat(value.replace("Z", "+00:00"))`` through the
adapters, all parsing goes through here so the quirks are documented, tested
and correctable in one place.

The eTenders "Z" defect (P0, resolved 16 Aug 2026)
--------------------------------------------------
The National Treasury eTender OCDS API serializes **SAST wall-clock times
with a literal ``Z`` suffix** — i.e. the offset is a lie. Evidence:

1. Every observed time-of-day sits on an SA business boundary: closings at
   10:00 / 11:00 / 12:00, briefings at 09:30 / 10:30 / 11:00. Genuine UTC
   would render as 12:00 / 13:00 / 14:00 SAST, which no SCM office uses.
2. Empty briefing sessions serialize as ``0001-01-01T00:00:00Z`` — .NET's
   ``DateTime.MinValue`` with a ``Z`` glued on. A real UTC conversion of an
   unspecified-kind ``DateTime`` cannot produce that; string concatenation
   can. This is the smoking gun: the publisher appends ``Z``, it does not
   convert.
3. Per-tender cross-checks against the buyers' own adverts match the API
   digits exactly (City of Cape Town 10:00, Theewaterskloof 12:00, KZN
   Public Works 11:00), and National Treasury's own advert pages write
   "11h00 (SAST)".

So ``2026-09-16T11:00:00Z`` from that portal means **11:00 SAST =
09:00 UTC**. We reinterpret it (`parse_wall_time_as_sast`) and mark the
affected fields ``DERIVED`` in provenance — the correction is disclosed,
never silent (§10.2.5).

Sentinels
---------
``0001-01-01T00:00:00Z`` (and anything in year 1) is "no value", not an
instant. It becomes ``None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: South African Standard Time. Fixed +02:00 — SA observes no DST.
SAST = timezone(timedelta(hours=2), "SAST")

#: Values at or below this are .NET ``DateTime.MinValue`` style sentinels.
_SENTINEL_YEAR = 1

#: Suffixes that mean "the publisher claims UTC".
_UTC_SUFFIXES = ("Z", "z", "+00:00", "+0000")


def is_sentinel(dt: datetime | None) -> bool:
    """True for ``0001-01-01``-style "no value" placeholders."""
    return dt is not None and dt.year <= _SENTINEL_YEAR


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string, honouring whatever offset it declares.

    Returns ``None`` for empty input, unparseable input, or a
    ``DateTime.MinValue`` sentinel. The result may be naive — callers that
    need a guarantee should use :func:`ensure_tz`.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if is_sentinel(parsed) else parsed


def ensure_tz(dt: datetime | None, *, assume: timezone = SAST) -> datetime | None:
    """Attach a zone to a naive datetime (§10.2.1: never store naive)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=assume) if dt.tzinfo is None else dt


def claims_utc(value: str) -> bool:
    """True when the ISO string declares UTC (``Z`` or ``+00:00``)."""
    return value.strip().endswith(_UTC_SUFFIXES)


def parse_wall_time_as_sast(value: str | None) -> tuple[datetime | None, bool]:
    """Parse a publisher timestamp that mislabels SAST wall time as UTC.

    Returns ``(instant, corrected)`` where ``corrected`` is True when we
    overrode the declared zone — callers stamp ``DERIVED`` provenance in
    that case so the change is visible to users and reviewers.

    * ``"2026-09-16T11:00:00Z"``      -> 11:00 SAST (09:00 UTC), corrected
    * ``"2026-09-16T11:00:00+02:00"`` -> 11:00 SAST, NOT corrected (the
      publisher stated a real offset; we trust an explicit non-UTC offset
      because it cannot be the artefact of a blind ``Z`` suffix)
    * ``"2026-09-16T11:00:00"``       -> 11:00 SAST, not corrected (naive
      input is SAST by house rule, §10.2.1)
    * ``"0001-01-01T00:00:00Z"``      -> ``(None, False)``
    """
    if not value:
        return None, False
    parsed = parse_iso(value)
    if parsed is None:
        return None, False

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SAST), False

    if claims_utc(value):
        # Strip the bogus zone and re-attach the real one; the wall-clock
        # digits are what the SCM office published.
        return parsed.replace(tzinfo=SAST), True

    return parsed, False


def to_sast(dt: datetime | None) -> datetime | None:
    """Render an instant in SAST for display/formatting."""
    return None if dt is None else ensure_tz(dt).astimezone(SAST)


def looks_date_only(dt: datetime | None) -> bool:
    """True when an instant is really "a date" — i.e. midnight somewhere.

    Publishers signal "no time given" by emitting midnight. Which midnight
    depends on how they (mis)handle zones: eTenders means midnight SAST,
    a correctly-zoned feed means midnight UTC. Either way there is no
    published closing TIME, so the SCM 11:00 convention applies (§10.2.1).
    A genuine 00:00 or 02:00 SAST deadline does not exist in SA procurement,
    so this heuristic has no realistic false positives.
    """
    if dt is None:
        return False
    aware = ensure_tz(dt)
    midnight = datetime.min.time()
    return (
        aware.astimezone(SAST).time() == midnight
        or aware.astimezone(timezone.utc).time() == midnight
    )
