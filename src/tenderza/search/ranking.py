"""Result ranking for tender search (Blueprint §12).

§12 asks for "keyword match + vector cosine + authority boost for official
sources; results sorted by match score and deadline". This module owns the
SQL expression for that score so the weighting lives in one reviewable place
instead of being spread through query strings.

The score is a weighted sum of three normalised-to-0..1 signals:

  keyword    ts_rank_cd of the FTS match. Uses the *unexpanded* user query so
             that a synonym hit ranks below a literal hit — expansion buys
             recall, ranking protects precision.
  authority  tenders.authority_score / 100. An official portal (eTenders,
             a municipal site) outranks an aggregator's copy of the same
             notice, which matters because aggregators often have staler
             closing dates (§8).
  urgency    a deadline curve. §12 wants deadline in the sort, but a plain
             `ORDER BY closing_at` buries a perfect match closing next month
             under a poor match closing tomorrow. Instead urgency is a
             *component*: tenders closing within the actionable window score
             highest, already-closed ones score zero.

Vector cosine is the fourth term (§12). The column exists
(`tender_embeddings`) and `semantic.py` fills it, but embeddings require a
model that is not available in this environment, so `KEYWORD_ONLY` weights
are used when no embedding is supplied and the vector term is simply absent
rather than faked. See `search_weights()`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tenders closing beyond this are "not urgent yet" — the curve flattens.
URGENCY_HORIZON_DAYS = 30
# Below this many days the tender is barely actionable: a supplier cannot
# assemble a compliant bid overnight, so urgency stops rewarding it.
URGENCY_FLOOR_HOURS = 12


@dataclass(frozen=True)
class Weights:
    """Relative contribution of each ranking signal. Need not sum to 1."""

    keyword: float = 1.0
    authority: float = 0.25
    urgency: float = 0.35
    vector: float = 0.0

    def normalised(self) -> Weights:
        total = self.keyword + self.authority + self.urgency + self.vector
        if total <= 0:
            return Weights()
        return Weights(
            keyword=self.keyword / total,
            authority=self.authority / total,
            urgency=self.urgency / total,
            vector=self.vector / total,
        )


# Keyword dominates: users search for words, and an authority boost strong
# enough to reorder relevance would make the engine feel broken ("why is this
# irrelevant eTenders notice first?"). Authority breaks near-ties, which is
# exactly the duplicate-across-sources case it exists for.
KEYWORD_ONLY = Weights(keyword=1.0, authority=0.25, urgency=0.35, vector=0.0)
# When embeddings are available, semantic similarity carries real weight but
# still less than a literal keyword hit.
HYBRID = Weights(keyword=1.0, authority=0.25, urgency=0.35, vector=0.6)


def search_weights(*, has_vector: bool = False) -> Weights:
    return HYBRID if has_vector else KEYWORD_ONLY


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

# ts_rank_cd rather than ts_rank: cover density rewards documents where the
# query terms appear close together, which suits title+description matches.
# The /(1+...) normalisation maps an unbounded rank into 0..1 so the weights
# mean what they say. Flag 32 is "rank/(rank+1)" — done in-engine.
KEYWORD_SQL = "ts_rank_cd(t.search_vector, {tsquery}, 32)"

AUTHORITY_SQL = "(coalesce(t.authority_score, 50)::float / 100.0)"

# Piecewise urgency:
#   closed or missing deadline -> 0
#   inside the floor           -> small constant (still visible, not promoted)
#   within the horizon         -> linear ramp, nearer = higher
#   beyond the horizon         -> low constant
URGENCY_SQL = f"""
CASE
    WHEN t.closing_at IS NULL THEN 0.15
    WHEN t.closing_at <= now() THEN 0.0
    WHEN t.closing_at < now() + make_interval(hours => {URGENCY_FLOOR_HOURS})
        THEN 0.35
    WHEN t.closing_at < now() + make_interval(days => {URGENCY_HORIZON_DAYS})
        THEN 1.0 - (
            extract(epoch FROM (t.closing_at - now()))
            / {URGENCY_HORIZON_DAYS * 86400}.0
        )
    ELSE 0.1
END
""".strip()


def score_sql(*, tsquery: str, weights: Weights | None = None,
              vector_expr: str | None = None) -> str:
    """Build the ranking expression.

    `tsquery` is the SQL text of the tsquery to rank against (a placeholder
    such as ``websearch_to_tsquery('english', %s)``) — ranking always uses the
    user's literal query, never the synonym-expanded one.

    `vector_expr` is SQL yielding cosine *similarity* in 0..1, or None.
    """
    w = (weights or search_weights(has_vector=vector_expr is not None)).normalised()
    parts = [
        f"{w.keyword:.4f} * {KEYWORD_SQL.format(tsquery=tsquery)}",
        f"{w.authority:.4f} * {AUTHORITY_SQL}",
        f"{w.urgency:.4f} * ({URGENCY_SQL})",
    ]
    if vector_expr is not None and w.vector > 0:
        parts.append(f"{w.vector:.4f} * ({vector_expr})")
    return "(" + " + ".join(parts) + ")"


def browse_order_sql() -> str:
    """Ordering with no keyword query: deadline-first browsing.

    Authority still breaks ties so that when the same tender exists twice the
    official copy leads.

    The trailing `t.id` makes this a *total* order. Without it, rows that tie
    on every other key may come back in a different sequence for each page,
    so a paginating client sees some tenders twice and never sees others.
    """
    return (
        "t.closing_at ASC NULLS LAST, "
        f"{AUTHORITY_SQL} DESC, "
        "t.published_at DESC NULLS LAST, "
        "t.id ASC"
    )
