"""Tender-number normalization — the backbone of dedupe (Blueprint §7).

A spec'd, tested PURE function. Canonicalization rules:

1. Casefold.
2. Split on separators (spaces, slashes, dashes, dots, colons, parentheses,
   underscores, ...).
3. WITHIN each segment, strip leading zeros from digit runs so that
   "SCM 045/2026" == "SCM45/2026" -> "scm452026". Zero-stripping happens
   BEFORE segments are joined — otherwise "2026/07/0012" would merge into
   one digit run and the inner zeros would be unreachable.
4. Join segments with no separator.

The function must stay deterministic and side-effect free; behaviour is
pinned by the fixture corpus in tests/fixtures/tender_numbers.json — extend
the corpus whenever a new real-world format is encountered.
"""

from __future__ import annotations

import re

# Characters treated as separators in the wild: space / - . : ( ) _ , ; # tabs
_SEPARATORS = re.compile(r"[\s/\-.:()_,;#|\\]+")
# A run of digits (used for leading-zero stripping)
_DIGIT_RUN = re.compile(r"\d+")


def _strip_leading_zeros(match: re.Match[str]) -> str:
    run = match.group(0).lstrip("0")
    return run if run else "0"


def normalize_tender_number(raw: str | None) -> str:
    """Canonicalize a South African tender/bid number for dedupe.

    >>> normalize_tender_number("SCM 045/2026")
    'scm452026'
    >>> normalize_tender_number("SCM45/2026")
    'scm452026'
    >>> normalize_tender_number(None)
    ''
    """
    if not raw:
        return ""
    value = raw.strip().casefold()
    # Split on separators (rule 2), strip zeros per segment (rule 3), join (rule 4).
    segments = _SEPARATORS.split(value)
    return "".join(_DIGIT_RUN.sub(_strip_leading_zeros, seg) for seg in segments)
