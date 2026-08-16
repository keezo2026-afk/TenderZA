"""Rules-based field extraction with confidence (Blueprint §6) — v1.

Extracts from tender document text: tender/bid number, closing date+time,
briefing date + compulsory flag, CIDB grading, B-BBEE requirement, contact
email/phone, estimated value. Every field carries a confidence score;
callers merge these into notices at LOWER precedence than structured
listing data (a PDF never silently overrides the portal's closing date —
authority and provenance rules of §8/§10 apply downstream).

All pure functions over text; the regex corpus is pinned by fixture tests
against real SA tender-document phrasings. Dates are SAST unless the text
says otherwise (§10.2.1); date-only closings get the 11:00 SCM convention
and INFERRED provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time

from tenderza.timeutil import SAST

SCM_DEFAULT_CLOSING = time(11, 0)


@dataclass
class Extracted:
    value: object
    confidence: float
    evidence: str            # the matched snippet — review-queue context


@dataclass
class ExtractedFields:
    tender_number: Extracted | None = None
    closing_at: Extracted | None = None
    briefing_at: Extracted | None = None
    compulsory_briefing: Extracted | None = None
    cidb_grades: Extracted | None = None
    bbee_level: Extracted | None = None
    contact_email: Extracted | None = None
    contact_phone: Extracted | None = None
    value_estimated: Extracted | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, dict]:
        out = {}
        for name in ("tender_number", "closing_at", "briefing_at",
                     "compulsory_briefing", "cidb_grades", "bbee_level",
                     "contact_email", "contact_phone", "value_estimated"):
            ex = getattr(self, name)
            if ex is not None:
                value = ex.value
                if isinstance(value, datetime):
                    value = value.isoformat()
                out[name] = {"value": value, "confidence": ex.confidence,
                             "evidence": ex.evidence[:200]}
        return out


# ---------------------------------------------------------------------------
# Patterns (kept explicit; extended via the fixture corpus, never in place)
# ---------------------------------------------------------------------------

# Requires either a "No/Number/Ref" token or a direct colon after the
# keyword, so bare headings ("INVITATION TO TENDER") never consume the
# line that actually carries the reference. Digit lookahead rejects junk.
_TENDER_NO = re.compile(
    r"""\b(?:tender|bid|quotation|rfq|rfp|contract)
        (?:
            \s+(?:no\.?|number|ref(?:erence)?\.?|\#)\s*[:\-]?
          |
            \s*[:\-]
        )\s*
        (?=[A-Z0-9 /\-\.]{0,30}\d)
        ([A-Z0-9][A-Z0-9 /\-\.]{2,30}[A-Z0-9])""",
    re.IGNORECASE | re.VERBOSE,
)

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_RE = "|".join(_MONTHS)

# "15 September 2026", "15th of September 2026"
_DATE_WORDS = rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\s+(\d{{4}})"
# "2026-09-15", "15/09/2026", "15-09-2026"
_DATE_NUM = r"(?:(\d{4})[-/](\d{1,2})[-/](\d{1,2})|(\d{1,2})[-/](\d{1,2})[-/](\d{4}))"
_TIME = r"(?:at\s+)?(\d{1,2})[:h](\d{2})(?:\s*(am|pm))?"

_CLOSING_CTX = re.compile(
    rf"""(?:closing|closes?|submission\s+deadline|due)\s*
         (?:date|time|date\s+and\s+time)?\s*[:\-]?\s*
         (?:{_DATE_WORDS}|{_DATE_NUM})(?:[\s,]+{_TIME})?""",
    re.IGNORECASE | re.VERBOSE,
)

_BRIEFING_CTX = re.compile(
    rf"""((?<![no][nN][- ])(?:compulsory|mandatory)\s+)?
         (?:site\s+(?:meeting|inspection|briefing)|briefing(?:\s+session)?|
            clarification\s+meeting)
         .{{0,80}}?
         (?:{_DATE_WORDS}|{_DATE_NUM})(?:[\s,]+{_TIME})?""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# "non-compulsory" / "non compulsory" must never flag compulsory=True.
_COMPULSORY_NEAR_BRIEFING = re.compile(
    r"(?<!non-)(?<!non )(?:compulsory|mandatory).{0,60}"
    r"(?:briefing|site\s+(?:meeting|inspection))"
    r"|(?:briefing|site\s+(?:meeting|inspection)).{0,60}"
    r"(?<!non-)(?<!non )(?:compulsory|mandatory)",
    re.IGNORECASE | re.DOTALL,
)

# CIDB: "9CE", "7 GB", "6SQ or higher" — grade 1-9 + class of works code
_CIDB = re.compile(r"\b([1-9])\s*(CE|GB|ME|EP|EB|SB|SF|SH|SI|SJ|SK|SL|SO|SQ)\b")
_CIDB_CTX = re.compile(r"cidb|construction\s+industry\s+development", re.IGNORECASE)

_BBEE = re.compile(
    r"b[\s\-]?bbee\s*(?:status\s*)?(?:level|contributor\s+level)?\s*[:\-]?\s*"
    r"(?:level\s*)?([1-8])\b",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?:\+27|0)\s?\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b")

_VALUE = re.compile(
    r"(?:estimated\s+(?:value|cost|amount)|budget|contract\s+value)"
    r".{0,30}?R\s?([\d\s,\.]{4,20})",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date_groups(g: tuple, base: int) -> datetime | None:
    """Groups layout: words(day, month, year) then numeric(y,m,d | d,m,y)."""
    try:
        if g[base]:                                    # word date
            day = int(g[base])
            month = _MONTHS.index(g[base + 1].lower()) + 1
            year = int(g[base + 2])
        elif g[base + 3]:                              # yyyy-mm-dd
            year, month, day = int(g[base + 3]), int(g[base + 4]), int(g[base + 5])
        elif g[base + 6]:                              # dd/mm/yyyy
            day, month, year = int(g[base + 6]), int(g[base + 7]), int(g[base + 8])
        else:
            return None
        return datetime(year, month, day, tzinfo=SAST)
    except (ValueError, IndexError):
        return None


def _apply_time(dt: datetime, g: tuple, base: int) -> tuple[datetime, bool]:
    """Attach hh:mm if captured; returns (dt, time_found)."""
    try:
        if g[base]:
            hour, minute = int(g[base]), int(g[base + 1])
            if (g[base + 2] or "").lower() == "pm" and hour < 12:
                hour += 12
            return dt.replace(hour=hour, minute=minute), True
    except (ValueError, IndexError):
        pass
    return dt, False


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

def extract_fields(text: str) -> ExtractedFields:  # noqa: PLR0912, PLR0915
    out = ExtractedFields()
    if not text or not text.strip():
        out.warnings.append("empty text")
        return out

    # Tender number: first VALID match wins — keep iterating past junk like
    # "TENDER ... Tender No" header echoes that capture no digits.
    for m in _TENDER_NO.finditer(text[:8000]):
        candidate = m.group(1).strip().rstrip(".")
        if re.search(r"\d", candidate) and len(candidate) <= 30:
            near_top = m.start() < 2000
            out.tender_number = Extracted(candidate, 0.85 if near_top else 0.7,
                                          m.group(0))
            break

    # Closing date/time
    m = _CLOSING_CTX.search(text)
    if m:
        dt = _parse_date_groups(m.groups(), 0)
        if dt:
            dt, has_time = _apply_time(dt, m.groups(), 9)
            if not has_time:
                dt = dt.replace(hour=SCM_DEFAULT_CLOSING.hour,
                                minute=SCM_DEFAULT_CLOSING.minute)
            # PDF text is unstructured: cap below the review threshold used
            # for structured feeds; time present = stronger signal.
            out.closing_at = Extracted(dt, 0.75 if has_time else 0.6, m.group(0))

    # Briefing
    m = _BRIEFING_CTX.search(text)
    if m:
        dt = _parse_date_groups(m.groups(), 1)
        if dt:
            dt, _ = _apply_time(dt, m.groups(), 10)
            out.briefing_at = Extracted(dt, 0.7, m.group(0))
        compulsory = bool(m.group(1)) or bool(_COMPULSORY_NEAR_BRIEFING.search(text))
        out.compulsory_briefing = Extracted(compulsory, 0.8 if m.group(1) else 0.65,
                                            m.group(0)[:120])
    elif _COMPULSORY_NEAR_BRIEFING.search(text):
        out.compulsory_briefing = Extracted(True, 0.6,
                                            _COMPULSORY_NEAR_BRIEFING.search(text).group(0))

    # CIDB grades — require CIDB context somewhere in the document to avoid
    # matching product codes.
    if _CIDB_CTX.search(text):
        grades = sorted({f"{g}{c}" for g, c in _CIDB.findall(text)})
        if grades:
            out.cidb_grades = Extracted(grades, 0.8, f"grades {grades}")

    m = _BBEE.search(text)
    if m:
        out.bbee_level = Extracted(f"Level {m.group(1)}", 0.75, m.group(0))

    m = _EMAIL.search(text)
    if m:
        out.contact_email = Extracted(m.group(0).lower(), 0.9, m.group(0))

    m = _PHONE.search(text)
    if m:
        out.contact_phone = Extracted(re.sub(r"[\s\-]", "", m.group(0)), 0.8,
                                      m.group(0))

    m = _VALUE.search(text)
    if m:
        raw = re.sub(r"[\s,]", "", m.group(1)).rstrip(".")
        try:
            amount = float(raw)
            if 1_000 <= amount <= 10_000_000_000:      # sanity band (§6)
                out.value_estimated = Extracted(amount, 0.6, m.group(0)[:120])
        except ValueError:
            pass

    return out
