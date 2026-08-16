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

    def test_fts_query_uses_websearch(self):
        sql, params = build_search_query(q="cctv -maintenance")
        assert "websearch_to_tsquery" in sql
        assert "ts_rank" in sql
        assert params.count("cctv -maintenance") == 2  # WHERE + ORDER BY

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
