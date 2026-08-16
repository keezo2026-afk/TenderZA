"""Ranking, highlighting and semantic plumbing (Blueprint §12)."""

from __future__ import annotations

import pytest

from tenderza.search import ranking, semantic
from tenderza.search.highlight import (
    START_TAG,
    STOP_TAG,
    headline_sql,
    highlight_plain,
    is_safe_highlight,
    strip_marks,
)

TSQ = "websearch_to_tsquery('english', %s)"


class TestWeights:
    def test_normalised_sums_to_one(self):
        w = ranking.Weights(keyword=2, authority=1, urgency=1, vector=0).normalised()
        assert pytest.approx(w.keyword + w.authority + w.urgency + w.vector) == 1.0

    def test_keyword_dominates(self):
        # An authority boost strong enough to reorder relevance makes the
        # engine feel broken. Keyword must stay the largest single signal.
        w = ranking.KEYWORD_ONLY.normalised()
        assert w.keyword > w.authority + w.urgency

    def test_hybrid_adds_vector_without_overtaking_keyword(self):
        w = ranking.HYBRID.normalised()
        assert w.vector > 0
        assert w.keyword > w.vector

    def test_zero_weights_do_not_divide_by_zero(self):
        w = ranking.Weights(0, 0, 0, 0).normalised()
        assert w.keyword == ranking.Weights().keyword

    def test_search_weights_selects_by_availability(self):
        assert ranking.search_weights(has_vector=False).vector == 0
        assert ranking.search_weights(has_vector=True).vector > 0


class TestScoreSql:
    def test_includes_all_three_base_signals(self):
        sql = ranking.score_sql(tsquery=TSQ)
        assert "ts_rank_cd" in sql
        assert "authority_score" in sql
        assert "closing_at" in sql

    def test_vector_term_absent_without_embedding(self):
        # Must be omitted, not zero-filled: a fake 0 would drag every score.
        assert "<=>" not in ranking.score_sql(tsquery=TSQ)

    def test_vector_term_present_with_embedding(self):
        sql = ranking.score_sql(tsquery=TSQ, vector_expr="(1 - (e.embedding <=> %s))")
        assert "<=>" in sql

    def test_one_placeholder_per_tsquery(self):
        assert ranking.score_sql(tsquery=TSQ).count("%s") == 1

    def test_authority_is_normalised_to_unit_range(self):
        # Raw 0..100 would swamp a 0..1 ts_rank.
        assert "/ 100.0" in ranking.score_sql(tsquery=TSQ)

    def test_null_authority_defaults_to_midpoint(self):
        assert "coalesce(t.authority_score, 50)" in ranking.score_sql(tsquery=TSQ)

    def test_browse_order_is_deadline_first(self):
        order = ranking.browse_order_sql()
        assert order.startswith("t.closing_at ASC NULLS LAST")
        # authority still breaks ties between duplicate notices
        assert "authority_score" in order


class TestHighlight:
    def test_headline_escapes_before_highlighting(self):
        sql = headline_sql("t.title", tsquery=TSQ, options="StartSel=x")
        # Escaping must happen inside the ts_headline call, on the column.
        assert "&lt;" in sql and "&amp;" in sql
        assert sql.index("replace") < sql.index(TSQ)

    def test_strip_marks(self):
        assert strip_marks(f"a {START_TAG}b{STOP_TAG} c") == "a b c"
        assert strip_marks("") == ""
        assert strip_marks(None) == ""

    def test_is_safe_highlight(self):
        assert is_safe_highlight(f"{START_TAG}road{STOP_TAG} works")
        assert is_safe_highlight("&lt;script&gt;")
        assert not is_safe_highlight("<script>alert(1)</script>")
        assert not is_safe_highlight("<b>bold</b>")

    def test_plain_highlighter_marks_terms(self):
        out = highlight_plain("Road maintenance", ["road"])
        assert out == f"{START_TAG}Road{STOP_TAG} maintenance"

    def test_plain_highlighter_escapes_html(self):
        out = highlight_plain("<script>road</script>", ["road"])
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert is_safe_highlight(out)

    def test_plain_highlighter_is_case_insensitive(self):
        assert START_TAG in highlight_plain("ROAD works", ["road"])

    def test_plain_highlighter_prefers_longest_term(self):
        out = highlight_plain("access control", ["access", "access control"])
        assert out == f"{START_TAG}access control{STOP_TAG}"

    def test_plain_highlighter_no_terms(self):
        assert highlight_plain("road", []) == "road"

    def test_plain_highlighter_empty_text(self):
        assert highlight_plain("", ["road"]) == ""


class _FakeEmbedder:
    model_name = "fake-1"

    def __init__(self, dim=semantic.EMBEDDING_DIM):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1] * self.dim for _ in texts]


class TestSemantic:
    def teardown_method(self):
        semantic.configure(None)

    def test_disabled_by_default(self):
        semantic.configure(None)
        assert semantic.is_enabled() is False
        assert semantic.active_embedder() is None

    def test_configure_enables(self):
        semantic.configure(_FakeEmbedder())
        assert semantic.is_enabled() is True

    def test_to_pgvector_format(self):
        lit = semantic.to_pgvector([1.0] * semantic.EMBEDDING_DIM)
        assert lit.startswith("[") and lit.endswith("]")
        assert lit.count(",") == semantic.EMBEDDING_DIM - 1

    def test_to_pgvector_rejects_wrong_dimension(self):
        # Silently storing a short vector would fail deep inside Postgres.
        with pytest.raises(ValueError, match="expected"):
            semantic.to_pgvector([0.1, 0.2])

    def test_embedding_text_puts_title_first(self):
        text = semantic.embedding_text(
            title="Road works", description="desc", buyer="City",
            document_text="doc",
        )
        assert text.startswith("Road works")
        assert text.index("doc") > text.index("desc")

    def test_embedding_text_truncates(self):
        text = semantic.embedding_text(title="x" * 10, document_text="y" * 10_000,
                                       max_chars=100)
        assert len(text) == 100

    def test_embedding_text_skips_missing_parts(self):
        assert semantic.embedding_text(title="Only") == "Only"

    def test_similarity_sql_clamps_at_zero(self):
        # Negative cosine similarity would subtract from a valid keyword hit.
        assert "greatest(0.0" in semantic.SIMILARITY_SQL

    def test_embed_pending_is_noop_when_disabled(self):
        semantic.configure(None)
        # Must not touch the connection at all.
        assert semantic.embed_pending(object()) == 0
