"""Vector (semantic) search over tenders (Blueprint §12).

Goal: "access control systems" should find a notice titled "supply and
installation of biometric entry readers", which no amount of synonym
expansion will ever cover.

Design position
---------------
Embeddings need an embedding model. This deployment has no model available
(no outbound network to a hosted API, no local model weights), so this module
is written as the *complete plumbing* around a pluggable `Embedder`:

  * the storage contract and SQL are real and tested;
  * the cosine-similarity expression is real and used by ranking;
  * the query path degrades to keyword-only, explicitly, when no embedder is
    configured — it never silently returns zeros that would corrupt ranking.

Swapping in a real model is implementing one method. That keeps §12's vector
requirement honest: the system is vector-ready and vector-shaped, and says so
rather than pretending to have semantics it does not have.

Distance vs similarity
----------------------
pgvector's `<=>` is cosine *distance* (0 = identical, 2 = opposite). Ranking
wants similarity in 0..1, hence `1 - (a <=> b)` clamped at 0 — negative
similarity (obtuse angle) is meaningless for recall and would subtract from
a keyword score that legitimately matched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# Matches the vector(1536) column in db/schema.sql. Changing this is a schema
# migration, not a config tweak, so it is asserted at write time.
EMBEDDING_DIM = 1536


class Embedder(Protocol):
    """Anything that turns text into a fixed-width vector."""

    @property
    def model_name(self) -> str:
        """Stable identifier stored alongside the vector.

        Rows are keyed (tender_id, model) so two models can coexist during a
        re-embedding migration.
        """
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


_EMBEDDER: Embedder | None = None


def configure(embedder: Embedder | None) -> None:
    """Install (or clear) the process-wide embedder."""
    global _EMBEDDER
    _EMBEDDER = embedder


def active_embedder() -> Embedder | None:
    return _EMBEDDER


def is_enabled() -> bool:
    """Whether semantic search can run. Callers must branch on this."""
    return _EMBEDDER is not None


def embedding_text(*, title: str, description: str | None = None,
                   buyer: str | None = None,
                   document_text: str | None = None,
                   max_chars: int = 6000) -> str:
    """Compose the text that represents a tender in vector space.

    Ordering matters: embedding models weight earlier tokens more heavily and
    truncate the tail, so the title (the densest signal) leads and the
    document text (long, boilerplate-heavy) trails.
    """
    parts = [title.strip()]
    if buyer:
        parts.append(buyer.strip())
    if description:
        parts.append(description.strip())
    if document_text:
        parts.append(document_text.strip())
    return "\n\n".join(p for p in parts if p)[:max_chars]


def to_pgvector(values: Sequence[float]) -> str:
    """Render a Python sequence as a pgvector literal.

    Done as text so the module has no hard dependency on the `pgvector`
    Python package — psycopg casts the string with `::vector`.
    """
    if len(values) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding has {len(values)} dims, expected {EMBEDDING_DIM}"
        )
    return "[" + ",".join(f"{float(v):.6g}" for v in values) + "]"


# Cosine similarity in 0..1 against a query vector placeholder.
SIMILARITY_SQL = (
    "greatest(0.0, 1.0 - (e.embedding <=> %s::vector))"
)

UPSERT_SQL = """
INSERT INTO tender_embeddings (tender_id, model, embedding)
VALUES (%s, %s, %s::vector)
ON CONFLICT (tender_id, model) DO UPDATE
    SET embedding = EXCLUDED.embedding, created_at = now()
"""


def store_embedding(conn, tender_id: str, model: str,
                    values: Sequence[float]) -> None:
    """Persist one embedding. Caller controls the transaction."""
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, (tender_id, model, to_pgvector(values)))


def embed_pending(conn, *, limit: int = 100) -> int:
    """Embed tenders that have no vector for the active model.

    Returns the number embedded; 0 when semantic search is not configured, so
    a scheduled job can call this unconditionally.
    """
    embedder = _EMBEDDER
    if embedder is None:
        return 0

    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT t.id, t.title, t.description, t.document_text,
                   o.name AS buyer_name
            FROM tenders t
            LEFT JOIN organisations o ON o.id = t.buyer_id
            WHERE NOT EXISTS (
                SELECT 1 FROM tender_embeddings e
                WHERE e.tender_id = t.id AND e.model = %s
            )
            ORDER BY t.closing_at ASC NULLS LAST
            LIMIT %s
            """,
            (embedder.model_name, limit),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    texts = [
        embedding_text(
            title=r["title"] or "",
            description=r["description"],
            buyer=r["buyer_name"],
            document_text=r["document_text"],
        )
        for r in rows
    ]
    vectors = embedder.embed(texts)
    if len(vectors) != len(rows):
        raise ValueError(
            f"embedder returned {len(vectors)} vectors for {len(rows)} tenders"
        )

    for row, vec in zip(rows, vectors, strict=True):
        store_embedding(conn, str(row["id"]), embedder.model_name, vec)
    return len(rows)
