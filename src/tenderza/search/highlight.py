"""Keyword highlighting for search results (Blueprint §12).

Postgres `ts_headline` does this well — it stems, so searching "cleaning"
highlights "cleaned", which naive client-side string matching cannot do. It
is, however, expensive: it re-parses the *document*, not the index, so it must
only ever run on the rows actually being returned (one page), never inside a
count query or a subquery that the planner might run over the whole table.

Output contract
---------------
The API returns highlight strings containing ``<mark>`` and nothing else —
the surrounding document text is HTML-escaped by ts_headline's caller here
(see `SNIPPET_OPTS`), so the frontend can render them without opening an XSS
hole. Any change to the delimiters must be mirrored in `web/lib/highlight.tsx`.
"""

from __future__ import annotations

import html
import re

START_TAG = "<mark>"
STOP_TAG = "</mark>"

# MaxFragments=0 asks for a single window around the best match, which reads
# better than stitched fragments for short tender descriptions.
SNIPPET_OPTS = (
    f"StartSel={START_TAG}, StopSel={STOP_TAG}, "
    "MaxWords=35, MinWords=15, ShortWord=3, HighlightAll=FALSE, "
    "MaxFragments=0"
)

# For the title we want the *whole* title back with matches marked, not a
# window — titles are short and truncating them looks broken.
TITLE_OPTS = (
    f"StartSel={START_TAG}, StopSel={STOP_TAG}, HighlightAll=TRUE"
)


def headline_sql(column: str, *, tsquery: str, options: str) -> str:
    """SQL for a highlighted rendering of `column`.

    The column is escaped *before* highlighting so that a tender whose title
    legitimately contains ``<script>`` cannot inject markup through the
    highlight path — ts_headline is not an HTML-aware function and will
    happily pass angle brackets through.
    """
    escaped = (
        f"replace(replace(replace(coalesce({column}, ''), '&', '&amp;'), "
        "'<', '&lt;'), '>', '&gt;')"
    )
    return (
        f"ts_headline('english', {escaped}, {tsquery}, "
        f"'{options}')"
    )


_TAG_RE = re.compile(r"</?mark>")


def strip_marks(text: str) -> str:
    """Remove highlight markup — for plain-text consumers such as email."""
    return _TAG_RE.sub("", text or "")


def is_safe_highlight(text: str) -> bool:
    """True when `text` contains no markup beyond our own <mark> tags.

    Used by the API tests as a guard: if this ever fails, escaping upstream
    of ts_headline has regressed and the frontend must stop trusting the
    string.
    """
    return "<" not in _TAG_RE.sub("", text or "")


def highlight_plain(text: str, terms: list[str]) -> str:
    """Pure-Python fallback highlighter.

    Used where a database round-trip is not warranted (email digests, tests).
    Exact/prefix matching only — no stemming, so it is strictly weaker than
    ts_headline and never used for the API response.
    """
    if not text or not terms:
        return html.escape(text or "")
    pattern = "|".join(
        re.escape(t) for t in sorted({t for t in terms if t}, key=len, reverse=True)
    )
    if not pattern:
        return html.escape(text)
    out: list[str] = []
    last = 0
    for m in re.finditer(pattern, text, re.IGNORECASE):
        out.append(html.escape(text[last:m.start()]))
        out.append(f"{START_TAG}{html.escape(m.group(0))}{STOP_TAG}")
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)
