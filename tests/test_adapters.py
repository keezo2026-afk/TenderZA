"""Adapter framework tests — registry contract + pure parsers with golden fixtures."""

import json
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
        assert notice.closing_at.isoformat() == "2026-09-16T11:00:00+00:00"
        assert len(notice.documents) == 1
        assert notice.documents[0].url.startswith("https://www.etenders.gov.za/")

    def test_minimal_release_does_not_crash(self):
        notice = release_to_notice({"ocid": "ocds-abc-1"}, "s", "u")
        assert notice.title == "ocds-abc-1"
        assert notice.closing_at is None
        assert notice.compulsory_briefing is None  # unknown, not False


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
