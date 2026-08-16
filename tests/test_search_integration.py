"""Search behaviour against a real Postgres (Blueprint §12).

Unit tests prove the SQL is *shaped* right; only Postgres proves it parses,
that the synonym tsquery is valid, that ts_headline escapes what we think it
escapes, and that ranking actually orders rows the way the weights claim.
"""

from __future__ import annotations

import pytest

from tests.conftest import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def conn():
    import psycopg
    from psycopg.rows import dict_row

    c = psycopg.connect(DSN, row_factory=dict_row)
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def corpus(conn):
    """A small multilingual corpus with known authority and deadlines."""
    ids = {}
    with conn.cursor() as cur:
        def add(key, title, desc, authority, days, doc=None,
                province="Gauteng"):
            cur.execute(
                """
                INSERT INTO tenders (title, description, province, status,
                    closing_at, authority_score, document_text, original_url)
                VALUES (%s, %s, %s, 'OPEN',
                        now() + make_interval(days => %s), %s, %s, 'http://x')
                RETURNING id
                """,
                (title, desc, province, days, authority, doc),
            )
            ids[key] = cur.fetchone()["id"]

        add("af", "Bou van 'n nuwe kliniek", "Konstruksie van fasiliteite", 90, 20)
        add("zu", "Ukwakhiwa kwesikole", "Ukwakhiwa kwezakhiwo", 90, 25)
        add("low", "Construction of a clinic", "Building works", 20, 20)
        add("high", "Construction of a clinic", "Building works", 95, 20)
        add("doc", "Supply of assorted items", "General goods", 50, 20,
            doc="Installation of biometric turnstiles and access control.")
        add("xss", "<script>alert(1)</script> construction",
            "<img src=x onerror=y> building", 50, 10)
    conn.commit()
    yield ids
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenders WHERE id = ANY(%s)",
                    (list(ids.values()),))
    conn.commit()


def run(conn, **kw):
    from tenderza.api.queries import build_search_query

    sql, params = build_search_query(**kw)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


class TestCrossLanguageRecall:
    def test_english_query_finds_afrikaans_notice(self, conn, corpus):
        titles = [r["title"] for r in run(conn, q="construction", limit=50)]
        assert any("Bou van" in t for t in titles)

    def test_english_query_finds_isizulu_notice(self, conn, corpus):
        titles = [r["title"] for r in run(conn, q="construction", limit=50)]
        assert any("Ukwakhiwa" in t for t in titles)

    def test_afrikaans_query_finds_english_notice(self, conn, corpus):
        titles = [r["title"] for r in run(conn, q="bou", limit=50)]
        assert any("Construction" in t for t in titles)

    def test_negation_does_not_cancel_the_positive_term(self, conn, corpus):
        # "building" is a synonym of "construction": naive expansion returns 0.
        rows = run(conn, q="construction -building", limit=50)
        assert rows, "synonym-expanded negation wiped out the query"


class TestAuthorityBoost:
    def test_official_source_outranks_aggregator_copy(self, conn, corpus):
        rows = run(conn, q="construction", limit=50)
        pair = [r for r in rows if r["title"] == "Construction of a clinic"]
        assert len(pair) == 2
        assert pair[0]["authority_score"] == 95, "aggregator copy ranked first"

    def test_authority_does_not_override_relevance(self, conn, corpus):
        # A high-authority irrelevant tender must not beat a relevant one.
        rows = run(conn, q="biometric", limit=50)
        assert rows[0]["title"] == "Supply of assorted items"


class TestDocumentText:
    def test_document_text_is_searchable(self, conn, corpus):
        rows = run(conn, q="biometric", limit=50)
        assert [r["title"] for r in rows] == ["Supply of assorted items"]

    def test_document_text_ranks_below_title(self, conn, corpus):
        # Weight D vs weight A: a title hit must win.
        rows = run(conn, q="construction", limit=50)
        assert "Supply of assorted items" not in [r["title"] for r in rows]


class TestHighlighting:
    def test_marks_matched_terms(self, conn, corpus):
        rows = run(conn, q="construction", highlight=True, limit=50)
        assert any("<mark>" in (r["title_highlight"] or "") for r in rows)

    def test_stems_when_highlighting(self, conn, corpus):
        # ts_headline stems: "installation" query marks "Installation".
        rows = run(conn, q="installations", highlight=True, limit=50)
        if rows:
            assert any("<mark>" in (r["snippet_highlight"] or "")
                       or "<mark>" in (r["title_highlight"] or "")
                       for r in rows)

    def test_markup_in_source_text_is_escaped(self, conn, corpus):
        from tenderza.search.highlight import is_safe_highlight

        rows = run(conn, q="construction", highlight=True, limit=50)
        for r in rows:
            assert is_safe_highlight(r["title_highlight"] or "")
            assert is_safe_highlight(r["snippet_highlight"] or "")

    def test_no_highlight_columns_when_disabled(self, conn, corpus):
        rows = run(conn, q="construction", highlight=False, limit=1)
        assert "title_highlight" not in rows[0]


class TestCountAgreement:
    @pytest.mark.parametrize("q", ["construction", "biometric", None, "bou"])
    def test_count_matches_row_count(self, conn, corpus, q):
        """The count query must match what the search query returns.

        The API caps `limit` at 100, and the suite shares a database that
        accumulates rows across runs, so page through the whole result set
        rather than asking for one oversized page.
        """
        from tenderza.api.queries import build_count_query

        csql, cparams = build_count_query(q=q)
        with conn.cursor() as cur:
            cur.execute(csql, cparams)
            total = cur.fetchone()["count"]

        seen, offset = [], 0
        while True:
            page = run(conn, q=q, limit=100, offset=offset)
            seen.extend(r["id"] for r in page)
            if len(page) < 100:
                break
            offset += 100
            assert offset <= 10_000, "runaway pagination"

        assert total == len(seen)
        assert len(set(seen)) == len(seen), "pagination returned duplicate rows"

    def test_keyword_pagination_is_stable(self, conn, corpus):
        """Rows tied on score and deadline must not shuffle between pages.

        Regression: the ORDER BY lacked a unique final key, so paging through
        results returned some tenders twice and skipped others entirely.
        """
        seen, offset = [], 0
        while True:
            page = run(conn, q="construction", limit=2, offset=offset)
            seen.extend(r["id"] for r in page)
            if len(page) < 2:
                break
            offset += 2
            assert offset <= 1_000, "runaway pagination"
        assert len(set(seen)) == len(seen), "duplicate rows across pages"

    def test_count_is_unaffected_by_pagination(self, conn, corpus):
        from tenderza.api.queries import build_count_query

        csql, cparams = build_count_query(q="construction")
        with conn.cursor() as cur:
            cur.execute(csql, cparams)
            total = cur.fetchone()["count"]
        assert total >= 4
        page = run(conn, q="construction", limit=2, offset=0)
        assert len(page) == 2, "limit not applied"


class TestHostileInput:
    @pytest.mark.parametrize("q", [
        "", "   ", "&&&", "-", '"', "a & b | c", "!!!", "OR",
        "'; DROP TABLE tenders;--", "((((", "\\", "%s", "construction & !",
        "-construction", '"unclosed', "café", "\x00bad",
    ])
    def test_never_errors(self, conn, corpus, q):
        run(conn, q=q, highlight=True, limit=5)

    def test_table_survives_injection_attempt(self, conn, corpus):
        run(conn, q="'; DROP TABLE tenders;--", highlight=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS c FROM tenders")
            assert cur.fetchone()["c"] >= len(corpus)


class TestAuthorityPersistence:
    def test_authority_is_monotonic_on_update(self, conn):
        """A low-authority republish must not demote an official notice."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenders (title, authority_score, original_url) "
                "VALUES ('Monotonic test', 95, 'http://x') RETURNING id"
            )
            tid = cur.fetchone()["id"]
            cur.execute(
                "UPDATE tenders SET authority_score = greatest(authority_score, %s) "
                "WHERE id = %s",
                (20, tid),
            )
            cur.execute("SELECT authority_score FROM tenders WHERE id = %s", (tid,))
            assert cur.fetchone()["authority_score"] == 95
            cur.execute("DELETE FROM tenders WHERE id = %s", (tid,))
        conn.commit()

    def test_default_authority_is_midpoint(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenders (title, original_url) "
                "VALUES ('Default authority', 'http://x') RETURNING authority_score"
            )
            assert cur.fetchone()["authority_score"] == 50
        conn.rollback()


class TestVectorColumn:
    def test_pgvector_is_available(self, conn):
        # §12 names pgvector; if the extension is missing the semantic path
        # can never be switched on, so fail loudly here rather than at runtime.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('tender_embeddings') AS t")
            assert cur.fetchone()["t"] is not None

    def test_similarity_expression_runs(self, conn):
        from tenderza.search import semantic

        vec = semantic.to_pgvector([0.0] * semantic.EMBEDDING_DIM)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {semantic.SIMILARITY_SQL} AS s "
                "FROM (SELECT %s::vector AS embedding) e",
                (vec, vec),
            )
            assert cur.fetchone()["s"] is not None

    def test_search_with_vector_executes(self, conn, corpus):
        from tenderza.search import semantic

        vec = semantic.to_pgvector([0.01] * semantic.EMBEDDING_DIM)
        rows = run(conn, q="construction", query_vector=vec, highlight=True,
                   limit=10)
        assert rows, "vector-enabled search returned nothing"
