"""Unit tests for the API query builder + row shaping (no DB needed)."""

import pytest

from tenderza.api.queries import (
    build_count_query,
    build_search_query,
    shape_tender_row,
)


class TestBuildSearchQuery:
    def test_no_filters(self):
        sql, params = build_search_query()
        assert "WHERE" not in sql
        assert "ORDER BY t.closing_at ASC" in sql
        assert params == [20, 0]

    def test_fts_matches_expanded_but_ranks_literal(self):
        # §12: synonym expansion buys recall (the WHERE clause), while ranking
        # stays on the user's literal query so a synonym hit never outranks a
        # literal one.
        sql, params = build_search_query(q="cctv -maintenance")
        assert "to_tsquery" in sql
        assert "ts_rank_cd" in sql
        # ranking + nothing else uses the raw string
        assert params.count("cctv -maintenance") == 1
        expanded = [p for p in params if isinstance(p, str) and "camera" in p]
        assert expanded, "matching should use the synonym-expanded tsquery"

    def test_unexpandable_query_falls_back_to_websearch(self):
        # Nothing to expand -> keep websearch_to_tsquery, which is more
        # forgiving of odd input than anything we would reconstruct.
        sql, params = build_search_query(q="zzzznotaword")
        assert "websearch_to_tsquery" in sql
        assert params.count("zzzznotaword") == 2  # WHERE + ORDER BY

    def test_all_filters_compose(self):
        sql, params = build_search_query(
            q="security", province="KwaZulu-Natal", status="OPEN",
            buyer="ethekwini", closing_within_days=30,
            compulsory_briefing=True, limit=50, offset=10,
        )
        assert sql.count("%s") == len(params)
        assert "t.province = %s" in sql
        assert "t.status = %s::tender_status" in sql
        assert "ILIKE" in sql
        assert "make_interval" in sql

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError, match="invalid status"):
            build_search_query(status="NOT_A_STATUS")

    def test_status_case_insensitive(self):
        sql, params = build_search_query(status="open")
        assert "OPEN" in params

    def test_invalid_province_rejected(self):
        with pytest.raises(ValueError, match="invalid province"):
            build_search_query(province="Atlantis")

    def test_limit_bounds(self):
        with pytest.raises(ValueError):
            build_search_query(limit=0)
        with pytest.raises(ValueError):
            build_search_query(limit=101)

    def test_count_query_params_match(self):
        for kwargs in (
            {},
            {"q": "cctv"},
            {"q": "cctv", "province": "Gauteng", "status": "OPEN"},
        ):
            sql, params = build_count_query(**kwargs)
            assert sql.count("%s") == len(params), kwargs
            assert sql.startswith("SELECT count(*)")


class TestShapeTenderRow:
    def _row(self, **kw):
        from datetime import datetime, timezone
        base = dict(
            id="11111111-1111-1111-1111-111111111111",
            tender_number="SCM 045/2026",
            title="CCTV", description=None, buyer_name="eThekwini",
            province="KwaZulu-Natal", status="OPEN",
            published_at=None,
            closing_at=datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc),
            briefing_at=None, compulsory_briefing=True,
            value_estimated=None, currency="ZAR",
            original_url="https://durban.gov.za/t/1",
            source_urls=["https://durban.gov.za/t/1"],
            field_provenance={
                "closing_at": {"source": "SOURCE", "source_id": "s", "confidence": 0.98}
            },
        )
        base.update(kw)
        return base

    def test_verified_closing_date(self):
        out = shape_tender_row(self._row())
        assert out["dates"]["closing_verified"] is True
        assert out["dates"]["verify_at_source"] is None

    def test_unverified_closing_date_links_to_source(self):
        """§10.3: unverified date -> verify-at-source, never silent."""
        row = self._row(field_provenance={
            "closing_at": {"source": "INFERRED", "source_id": "s", "confidence": 0.55}
        })
        out = shape_tender_row(row)
        assert out["dates"]["closing_verified"] is False
        assert out["dates"]["verify_at_source"] == "https://durban.gov.za/t/1"

    def test_missing_provenance_is_unverified(self):
        out = shape_tender_row(self._row(field_provenance={}))
        assert out["dates"]["closing_verified"] is False

    def test_derived_correction_is_disclosed_but_still_verified(self):
        """§10.2.5: a repaired closing time stays trustworthy AND explains itself."""
        row = self._row(field_provenance={"closing_at": {
            "source": "DERIVED", "source_id": "etenders-ocds", "confidence": 0.98,
            "note": "eTenders publishes SAST wall-clock times with a 'Z' suffix",
        }})
        out = shape_tender_row(row)["dates"]
        assert out["closing_verified"] is True
        assert out["closing_source"] == "DERIVED"
        assert "SAST" in out["closing_note"]

    def test_plain_source_dates_carry_no_note(self):
        out = shape_tender_row(self._row())["dates"]
        assert out["closing_source"] == "SOURCE"
        assert out["closing_note"] is None

    def test_attribution_always_present(self):
        out = shape_tender_row(self._row())
        assert out["original_url"]
        assert out["source_urls"]

class TestSearchV2:
    """§12 additions: authority boost, highlighting, vectors, doc text."""

    def test_ranking_includes_authority_boost(self):
        # The docstring used to promise this while the SQL had no such term.
        sql, _ = build_search_query(q="road")
        assert "authority_score" in sql.split("ORDER BY")[1]

    def test_browse_mode_is_deadline_first(self):
        sql, _ = build_search_query()
        order = sql.split("ORDER BY")[1]
        assert order.strip().startswith("t.closing_at ASC NULLS LAST")

    def test_highlight_off_by_default_in_builder(self):
        sql, _ = build_search_query(q="road")
        assert "ts_headline" not in sql

    def test_highlight_adds_two_columns(self):
        sql, _ = build_search_query(q="road", highlight=True)
        assert sql.count("ts_headline") == 2
        assert "AS title_highlight" in sql
        assert "AS snippet_highlight" in sql

    def test_highlight_ignored_without_query(self):
        # Nothing to highlight, and ts_headline is expensive.
        sql, _ = build_search_query(highlight=True)
        assert "ts_headline" not in sql

    def test_vector_join_only_when_vector_supplied(self):
        assert "tender_embeddings" not in build_search_query(q="road")[0]
        sql, _ = build_search_query(q="road", query_vector="[0.1]")
        assert "LEFT JOIN tender_embeddings" in sql
        assert "<=>" in sql

    def test_vector_join_is_left_join(self):
        # A tender with no embedding must remain findable by keyword.
        sql, _ = build_search_query(q="road", query_vector="[0.1]")
        assert "LEFT JOIN tender_embeddings" in sql

    @pytest.mark.parametrize("kwargs", [
        {},
        {"q": "road"},
        {"q": "road", "highlight": True},
        {"q": "road", "query_vector": "[0.1]"},
        {"q": "road", "highlight": True, "query_vector": "[0.1]"},
        {"q": "zzzznotaword", "highlight": True},
        {"highlight": True},
        {"q": "road", "province": "Gauteng", "status": "OPEN", "buyer": "city",
         "closing_within_days": 30, "compulsory_briefing": True,
         "highlight": True, "query_vector": "[0.1]"},
    ])
    def test_placeholders_match_params(self, kwargs):
        # Positional %s means ordering is load-bearing; a mismatch here is a
        # runtime error or, worse, silently swapped values.
        sql, params = build_search_query(**kwargs)
        assert sql.count("%s") == len(params), f"{sql}\n{params}"

    @pytest.mark.parametrize("q", ["", "   ", "&&&", "!!!", "OR", "-"])
    def test_meaningless_query_becomes_browse(self, q):
        # Otherwise "" returns everything but "  " returns nothing.
        sql, _ = build_search_query(q=q)
        assert "ts_rank_cd" not in sql

    def test_count_matches_search_filters(self):
        kw = dict(q="road", province="Gauteng", status="OPEN")
        csql, cparams = build_count_query(**kw)
        assert csql.count("%s") == len(cparams)
        assert "count(*)" in csql
        # A count must never pay for highlighting or the embedding join.
        assert "ts_headline" not in csql
        assert "tender_embeddings" not in csql

    def test_count_uses_same_tsquery_as_search(self):
        # If they diverge, the total disagrees with what a user can page to.
        _, sparams = build_search_query(q="construction")
        _, cparams = build_count_query(q="construction")
        expanded = [p for p in sparams if isinstance(p, str) and "bou" in p]
        assert expanded and expanded[0] in cparams

    def test_shape_row_exposes_authority(self):
        row = TestShapeTenderRow()._row(authority_score=95)
        assert shape_tender_row(row)["authority_score"] == 95

    def test_shape_row_omits_highlight_when_absent(self):
        # A null `highlight` key would imply the feature failed.
        assert "highlight" not in shape_tender_row(TestShapeTenderRow()._row())

    def test_shape_row_includes_highlight_when_present(self):
        row = TestShapeTenderRow()._row(
            title_highlight="<mark>CCTV</mark>", snippet_highlight=None)
        out = shape_tender_row(row)
        assert out["highlight"]["title"] == "<mark>CCTV</mark>"
        assert out["highlight"]["snippet"] is None
