"""Adapter framework tests — registry contract + pure parsers with golden fixtures."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tenderza.adapters import SourceConfig, get_adapter
from tenderza.adapters.base import known_adapters
from tenderza.adapters.generic_cms import wp_post_to_notice
from tenderza.adapters.generic_sitemap_rss import parse_feed, parse_sitemap
from tenderza.adapters.ocds_api import release_to_notice
from tenderza.adapters.pdf_bulletin import content_hash

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


def _cfg(adapter: str, **options) -> SourceConfig:
    return SourceConfig(
        source_id="src-test",
        name="Test Source",
        crawl_url="https://example.gov.za/tenders",
        adapter=adapter,
        options=options,
    )


class TestRegistry:
    def test_all_blueprint_adapters_registered(self):
        # §5: API + generic CMS + sitemap/RSS + PDF bulletin
        expected = {"ocds_api", "generic_cms", "generic_sitemap_rss", "pdf_bulletin"}
        assert expected.issubset(set(known_adapters()))

    def test_get_adapter_resolves(self):
        adapter = get_adapter(_cfg("ocds_api"))
        assert adapter.key == "ocds_api"
        assert adapter.config.source_id == "src-test"

    def test_unknown_adapter_raises(self):
        with pytest.raises(KeyError):
            get_adapter(_cfg("does_not_exist"))


class TestOcdsMapping:
    def test_release_to_notice(self):
        release = json.loads((FIXTURES / "ocds_api" / "sample_release.json").read_text())
        notice = release_to_notice(release, source_id="etenders-ocds", source_url="https://x")
        # eTenders convention: bid number lives in tender.title,
        # human-readable scope in tender.description (verified live 2026-08-14)
        assert notice.title == "Supply and Installation of CCTV System"
        assert notice.tender_number == "SCM 045/2026"
        assert notice.buyer_name == "eThekwini Metropolitan Municipality"
        assert notice.closing_at is not None
        assert notice.closing_at.tzinfo is not None  # never naive (§10.2.1)
        assert notice.raw == release  # verbatim archive for ocds_records (§10.2.6)
        assert len(notice.documents) == 1

    def test_real_release_captured_live(self):
        """Golden test against a REAL release fetched from the API on 2026-08-14."""
        payload = json.loads(
            (FIXTURES / "ocds_api" / "real_release_2026-08-14.json").read_text()
        )
        notice = release_to_notice(payload["release"], "etenders-ocds", "https://x")
        assert notice.tender_number == "ZNTM01266W"
        assert notice.title.startswith("Department of Education: Sanitation Programme")
        assert notice.buyer_name == "Kwazulu Natal - Public Works (Head Office)"
        # briefingSession extension -> compulsory-briefing flag (§10.2.2)
        assert notice.compulsory_briefing is True
        assert notice.briefing_at is not None
        assert notice.briefing_at.tzinfo is not None
        assert notice.province == "KwaZulu-Natal"     # eTenders extension
        assert notice.categories == ["Sewerage"]
        # The portal published "2026-09-16T11:00:00Z" — which on this source
        # means 11:00 SAST, i.e. 09:00 UTC (see ocds_api module docstring).
        assert notice.closing_at.isoformat() == "2026-09-16T11:00:00+02:00"
        assert notice.closing_at.astimezone(timezone.utc) == datetime(
            2026, 9, 16, 9, 0, tzinfo=timezone.utc
        )
        assert len(notice.documents) == 1
        assert notice.documents[0].url.startswith("https://www.etenders.gov.za/")

    def test_minimal_release_does_not_crash(self):
        notice = release_to_notice({"ocid": "ocds-abc-1"}, "s", "u")
        assert notice.title == "ocds-abc-1"
        assert notice.closing_at is None
        assert notice.compulsory_briefing is None  # unknown, not False
        assert notice.derived_fields == {}


class TestOcdsTimezoneDefect:
    """P0: eTenders stamps SAST wall times with a "Z" (see ocds_api docstring)."""

    def _release(self, **tender):
        base = {
            "ocid": "ocds-9t57fa-1",
            "date": "2026-08-14T00:00:00Z",
            "tender": {"title": "T1/2026", "description": "Something", **tender},
        }
        return base

    def test_closing_time_is_sast_not_utc(self):
        notice = release_to_notice(
            self._release(tenderPeriod={"endDate": "2026-09-16T11:00:00Z"}),
            "etenders-ocds", "https://x",
        )
        assert notice.closing_at.utcoffset() == timedelta(hours=2)
        assert notice.closing_at.hour == 11          # 11:00 SAST as advertised
        assert notice.closing_at.astimezone(timezone.utc).hour == 9

    def test_correction_is_recorded_as_derived(self):
        notice = release_to_notice(
            self._release(tenderPeriod={"endDate": "2026-09-16T11:00:00Z"}),
            "etenders-ocds", "https://x",
        )
        assert "closing_at" in notice.derived_fields
        assert "SAST" in notice.derived_fields["closing_at"]

    def test_briefing_time_is_corrected_too(self):
        notice = release_to_notice(
            self._release(briefingSession={
                "isSession": True, "compulsory": True,
                "date": "2026-08-25T10:30:00Z", "venue": "Gallwey House",
            }),
            "etenders-ocds", "https://x",
        )
        assert (notice.briefing_at.hour, notice.briefing_at.minute) == (10, 30)
        assert notice.briefing_at.utcoffset() == timedelta(hours=2)
        assert "briefing_at" in notice.derived_fields

    def test_empty_briefing_sentinel_is_not_a_date(self):
        """0001-01-01T00:00:00Z is .NET DateTime.MinValue, i.e. "no session"."""
        notice = release_to_notice(
            self._release(briefingSession={
                "isSession": True, "compulsory": False,
                "date": "0001-01-01T00:00:00Z", "venue": "N/A",
            }),
            "etenders-ocds", "https://x",
        )
        assert notice.briefing_at is None
        assert "briefing_at" not in notice.derived_fields

    def test_no_session_means_no_briefing_at_all(self):
        notice = release_to_notice(
            self._release(briefingSession={
                "isSession": False, "date": "0001-01-01T00:00:00Z",
            }),
            "etenders-ocds", "https://x",
        )
        assert notice.briefing_at is None
        assert notice.compulsory_briefing is None

    def test_published_date_keeps_its_calendar_day(self):
        """release.date is date-only; re-zoning must not shift it a day."""
        notice = release_to_notice(self._release(), "etenders-ocds", "https://x")
        assert notice.published_at.date().isoformat() == "2026-08-14"
        assert notice.published_at.utcoffset() == timedelta(hours=2)

    def test_missing_period_is_none_not_epoch(self):
        notice = release_to_notice(self._release(), "etenders-ocds", "https://x")
        assert notice.closing_at is None
        assert "closing_at" not in notice.derived_fields


class TestFeedParsing:
    def test_rss(self):
        xml = (FIXTURES / "generic_sitemap_rss" / "sample_rss.xml").read_text()
        notices = parse_feed(xml, "src-test", "https://example.gov.za/feed")
        assert len(notices) == 2
        assert notices[0].title == "Tender SCM 045/2026: CCTV Supply and Installation"
        assert notices[0].published_at is not None

    def test_atom(self):
        xml = (FIXTURES / "generic_sitemap_rss" / "sample_atom.xml").read_text()
        notices = parse_feed(xml, "src-test", "https://example.gov.za/atom")
        assert len(notices) == 1
        assert notices[0].source_url == "https://example.gov.za/tenders/road-maintenance"

    def test_sitemap_filter(self):
        xml = (FIXTURES / "generic_sitemap_rss" / "sample_sitemap.xml").read_text()
        urls = parse_sitemap(xml, url_filter="tender")
        assert urls == [
            "https://example.gov.za/tenders/scm-045-2026",
            "https://example.gov.za/tenders/rfq-2026-07-0012",
        ]


class TestWordPressMapping:
    def test_wp_post_to_notice(self):
        post = json.loads((FIXTURES / "generic_cms" / "sample_wp_post.json").read_text())
        notice = wp_post_to_notice(post, "src-test")
        assert "Grass Cutting" in notice.title
        assert notice.source_url.startswith("https://")
        assert notice.published_at is not None


class TestPdfBulletin:
    def test_content_hash_stable(self):
        assert content_hash(b"bulletin") == content_hash(b"bulletin")
        assert content_hash(b"bulletin-v2") != content_hash(b"bulletin")
