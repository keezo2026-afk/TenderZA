"""Search/filter SQL for the read API (Blueprint §12).

Pure query-building + row-shaping; no FastAPI imports so the module is
unit-testable without the web stack.

Ranking (§12): a weighted score over keyword match (ts_rank_cd), authority
of the source the tender came from, and deadline urgency — see
`tenderza.search.ranking`, which owns the weights. Vector cosine joins the
same expression when embeddings are configured
(`tenderza.search.semantic`). With no query, results are deadline-first.

Queries are expanded with the EN/AF/isiZulu synonym dictionary
(`tenderza.search.synonyms`) for recall, while *ranking* always uses the
user's literal query so a synonym hit never outranks a literal one.
"""

from __future__ import annotations

from typing import Any

from tenderza.search import ranking
from tenderza.search.highlight import SNIPPET_OPTS, TITLE_OPTS, headline_sql
from tenderza.search.synonyms import expand_to_tsquery, sanitize

VALID_STATUSES = {
    "NEW", "OPEN", "CLOSING_SOON", "CLOSED", "EXTENDED",
    "CANCELLED", "AWARDED", "RE_ADVERTISED", "UNKNOWN",
}

PROVINCES = {
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "North West", "Northern Cape", "Western Cape",
}

_COLUMNS = """
SELECT t.id, t.tender_number, t.title, t.description,
       t.province, t.status::text AS status,
       t.published_at, t.closing_at, t.briefing_at, t.compulsory_briefing,
       t.value_estimated, t.currency, t.original_url, t.source_urls,
       t.field_provenance, t.authority_score, o.name AS buyer_name
"""

_FROM = """
FROM tenders t
LEFT JOIN organisations o ON o.id = t.buyer_id
"""

_SELECT = _COLUMNS + _FROM

# The tsquery used for MATCHING: synonym-expanded, so an Afrikaans notice is
# found by an English query. to_tsquery (not websearch_) because we build the
# operator string ourselves; every lexeme in it is quoted, so user input can
# never be parsed as an operator.
_MATCH_EXPANDED = "t.search_vector @@ to_tsquery('english', %s)"
# The tsquery used for MATCHING when expansion added nothing: websearch_ is
# more forgiving of unusual input than anything we would reconstruct.
_MATCH_PLAIN = "t.search_vector @@ websearch_to_tsquery('english', %s)"
# The tsquery used for RANKING and HIGHLIGHTING: always the literal query.
_RANK_TSQUERY = "websearch_to_tsquery('english', %s)"


def build_search_query(
    *,
    q: str | None = None,
    province: str | None = None,
    status: str | None = None,
    buyer: str | None = None,
    closing_within_days: int | None = None,
    compulsory_briefing: bool | None = None,
    limit: int = 20,
    offset: int = 0,
    highlight: bool = False,
    query_vector: str | None = None,
) -> tuple[str, list[Any]]:
    """Return (sql, params) for the tender search. Raises ValueError on
    invalid filter values — the API layer maps that to HTTP 422.

    `highlight` adds ts_headline columns for the returned page only (§12).
    `query_vector` is a pgvector literal enabling the semantic term; when it
    is None the vector signal is omitted rather than zero-filled.
    """
    if status is not None:
        status = status.upper()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; one of {sorted(VALID_STATUSES)}")
    if province is not None and province not in PROVINCES:
        raise ValueError(f"invalid province {province!r}; one of {sorted(PROVINCES)}")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    # Synonym expansion happens once, up front: both the WHERE clause and the
    # count query must use the identical tsquery or the total will disagree
    # with the number of rows a user can page through.
    # Sanitize once, up front: `q` is also bound directly as a parameter on
    # the ranking/highlight/fallback paths, so stripping it only inside the
    # tokenizer would still let a NUL byte reach the driver.
    q = sanitize(q) if q else q
    expanded_tsquery, did_expand = expand_to_tsquery(q) if q else (None, False)
    if q and expanded_tsquery is None:
        # The query contained no searchable terms at all (whitespace, stray
        # punctuation, a lone "OR"). Treat it exactly like no query — browse
        # mode — rather than running a tsquery that matches nothing. Without
        # this, "" returns every tender but "  " returns none, which looks
        # like the search silently broke.
        q = None
    use_expanded = bool(q) and did_expand and expanded_tsquery is not None

    select_cols = _COLUMNS
    select_params: list[Any] = []
    if q and highlight:
        # Highlighting is scored against the literal query and applied only to
        # the rows this page returns.
        select_cols = (
            _COLUMNS
            + ", " + headline_sql("t.title", tsquery=_RANK_TSQUERY,
                                  options=TITLE_OPTS) + " AS title_highlight"
            + ", " + headline_sql("t.description", tsquery=_RANK_TSQUERY,
                                  options=SNIPPET_OPTS) + " AS snippet_highlight"
        )
        select_params.extend([q, q])

    where: list[str] = []
    params: list[Any] = []

    if q:
        if use_expanded:
            where.append(_MATCH_EXPANDED)
            params.append(expanded_tsquery)
        else:
            # websearch_to_tsquery: user-friendly syntax ("cctv -maintenance",
            # quoted phrases), no crash on stray operators.
            where.append(_MATCH_PLAIN)
            params.append(q)
    if province:
        where.append("t.province = %s")
        params.append(province)
    if status:
        where.append("t.status = %s::tender_status")
        params.append(status)
    if buyer:
        where.append("o.name ILIKE %s")
        params.append(f"%{buyer}%")
    if closing_within_days is not None:
        where.append(
            "t.closing_at IS NOT NULL AND t.closing_at BETWEEN now() "
            "AND now() + make_interval(days => %s)"
        )
        params.append(closing_within_days)
    if compulsory_briefing is not None:
        where.append("t.compulsory_briefing = %s")
        params.append(compulsory_briefing)

    from_clause = _FROM
    vector_expr: str | None = None
    if query_vector is not None:
        # LEFT JOIN: a tender without an embedding must still be findable by
        # keyword. coalesce below turns the resulting NULL into a neutral 0.
        # The join itself is parameterless — the query vector appears only in
        # the ORDER BY score expression, and is bound there.
        from_clause = _FROM + (
            " LEFT JOIN tender_embeddings e ON e.tender_id = t.id"
        )
        vector_expr = "coalesce(greatest(0.0, 1.0 - (e.embedding <=> %s::vector)), 0.0)"

    sql = select_cols + from_clause
    # Param order must follow the order placeholders appear in the SQL text:
    # SELECT list (highlighting), then WHERE, then ORDER BY.
    ordered: list[Any] = [*select_params, *params]

    if where:
        sql += " WHERE " + " AND ".join(where)

    if q:
        score = ranking.score_sql(tsquery=_RANK_TSQUERY, vector_expr=vector_expr)
        # The vector placeholder inside `score` repeats the query vector.
        # `t.id` last makes the order total so pagination cannot repeat or
        # skip rows that tie on both score and deadline (§12).
        sql += (
            f" ORDER BY {score} DESC, t.closing_at ASC NULLS LAST, t.id ASC"
        )
        ordered.append(q)
        if vector_expr is not None:
            ordered.append(query_vector)
    else:
        sql += f" ORDER BY {ranking.browse_order_sql()}"

    sql += " LIMIT %s OFFSET %s"
    ordered.extend([limit, offset])
    return sql, ordered


def build_count_query(
    *,
    q: str | None = None,
    province: str | None = None,
    status: str | None = None,
    buyer: str | None = None,
    closing_within_days: int | None = None,
    compulsory_briefing: bool | None = None,
) -> tuple[str, list[Any]]:
    # Built independently of build_search_query rather than by string-surgery
    # on its output: the search SQL now carries placeholders in the SELECT
    # list (highlighting) and in a JOIN (vectors), so slicing its parameter
    # list by position was silently wrong. A count must also never pay for
    # ts_headline or the embedding join — neither can change how many rows
    # match, and ts_headline over the whole result set is ruinously slow.
    sql, params = build_search_query(
        q=q, province=province, status=status, buyer=buyer,
        closing_within_days=closing_within_days,
        compulsory_briefing=compulsory_briefing,
        limit=1, offset=0,
    )
    body = sql.split(" ORDER BY ")[0]
    # Drop the trailing ORDER BY/LIMIT params: with highlight=False and no
    # vector, the only params after the WHERE clause are the ranking tsquery
    # (when q is set) plus limit and offset.
    trailing = 3 if q else 2
    return f"SELECT count(*) FROM ({body}) sub", params[: len(params) - trailing]


def shape_tender_row(row: dict[str, Any]) -> dict[str, Any]:
    """DB row -> API tender object. Enforces the §10.3 trust rule: an
    unverified closing date is presented as verify-at-source."""
    provenance = row.get("field_provenance") or {}
    closing_prov = provenance.get("closing_at") or {}
    closing_verified = (closing_prov.get("confidence") or 0.0) >= 0.80
    # §10.2.5: when we repaired a publisher's value (e.g. eTenders stamping
    # SAST wall times with a "Z"), say so next to the date rather than
    # presenting the corrected instant as if it came straight from source.
    closing_note = closing_prov.get("note") if closing_prov.get("source") in (
        "DERIVED", "INFERRED"
    ) else None

    return {
        "id": str(row["id"]),
        "tender_number": row["tender_number"],
        "title": row["title"],
        "description": row["description"],
        "buyer": row["buyer_name"],
        "province": row["province"],
        "status": row["status"],
        "dates": {
            "published_at": _iso(row["published_at"]),
            "closing_at": _iso(row["closing_at"]),
            "closing_verified": closing_verified,
            "closing_source": closing_prov.get("source"),
            "closing_note": closing_note,
            "verify_at_source": None if closing_verified else row["original_url"],
            "briefing_at": _iso(row["briefing_at"]),
            "compulsory_briefing": row["compulsory_briefing"],
        },
        "value_estimated": (
            float(row["value_estimated"]) if row["value_estimated"] is not None else None
        ),
        "currency": row["currency"],
        "original_url": row["original_url"],
        "source_urls": row["source_urls"] or [],
        # §12: authority of the most authoritative source this tender was seen
        # on. Exposed so the UI can explain ranking ("official source").
        "authority_score": row.get("authority_score"),
        **_highlight_fields(row),
    }


def _highlight_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Highlight columns, present only when the caller asked for them.

    Kept out of the main dict so a non-highlighted response has no null
    `highlight` key implying the feature failed.
    """
    title_hl = row.get("title_highlight")
    snippet_hl = row.get("snippet_highlight")
    if title_hl is None and snippet_hl is None:
        return {}
    return {
        "highlight": {
            # ts_headline returns the source text unchanged when nothing
            # matched; that is still the correct thing to render.
            "title": title_hl,
            "snippet": snippet_hl or None,
        }
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
