"""Pipeline tests: normalizer, entity resolution, dedupe/authority merge, status."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tenderza.adapters.base import RawTenderNotice
from tenderza.adapters.ocds_api import release_to_notice
from tenderza.pipeline import compute_status, fingerprint, merge_tenders, normalize_notice
from tenderza.pipeline.dedupe import are_duplicates, titles_similar
from tenderza.pipeline.entity_resolution import OrgResolver, normalize_org_name
from tenderza.pipeline.normalizer import SAST

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
UTC = timezone.utc


def _notice(**kw) -> RawTenderNotice:
    defaults = dict(
        source_id="src-a",
        source_url="https://a.gov.za/t/1",
        title="Supply and Installation of CCTV System",
        tender_number="SCM 045/2026",
        buyer_name="eThekwini Metropolitan Municipality",
        closing_at=datetime(2026, 9, 15, 11, 0, tzinfo=SAST),
    )
    defaults.update(kw)
    return RawTenderNotice(**defaults)


class TestNormalizer:
    def test_structured_source_high_confidence(self):
        t = normalize_notice(_notice(), authority_score=100)
        assert t.normalized_tender_number == "scm452026"
        assert t.confidence_for("closing_at") >= 0.9
        assert t.field_provenance["closing_at"]["source"] == "SOURCE"
        assert t.review_items == []  # nothing below threshold

    def test_scm_closing_time_convention(self):
        """Date-only closing -> 11:00 SAST, flagged INFERRED (§10.2.1)."""
        t = normalize_notice(
            _notice(closing_at=datetime(2026, 9, 15, 0, 0, tzinfo=SAST))
        )
        assert t.closing_at.hour == 11
        assert t.closing_at.tzinfo is not None
        assert t.field_provenance["closing_at"]["source"] == "INFERRED"

    def test_unstructured_high_stakes_goes_to_review(self):
        """PDF-extracted fields below threshold route to the review queue (§6)."""
        t = normalize_notice(_notice(), structured=False)
        fields = {item["field"] for item in t.review_items}
        assert "closing_at" in fields

    def test_naive_datetimes_get_sast(self):
        t = normalize_notice(_notice(closing_at=datetime(2026, 9, 15, 14, 30)))
        assert t.closing_at.tzinfo is not None
        assert t.closing_at.utcoffset() == timedelta(hours=2)

    def test_utc_midnight_closing_also_hits_the_scm_convention(self):
        """Midnight in ANY zone means "no time published" (§10.2.1)."""
        from datetime import timezone as _tz
        t = normalize_notice(
            _notice(closing_at=datetime(2026, 9, 15, 0, 0, tzinfo=_tz.utc))
        )
        assert t.closing_at.astimezone(SAST).hour == 11
        assert t.field_provenance["closing_at"]["source"] == "INFERRED"


class TestDerivedProvenance:
    """Adapter-level repairs of bad publisher data must be disclosed (§10.2.5)."""

    def _repaired(self, **fields):
        return normalize_notice(_notice(derived_fields=fields))

    def test_repaired_field_is_derived_not_source(self):
        t = self._repaired(closing_at="re-interpreted as SAST (+02:00)")
        assert t.field_provenance["closing_at"]["source"] == "DERIVED"

    def test_repair_note_is_carried_for_the_ui(self):
        t = self._repaired(closing_at="re-interpreted as SAST (+02:00)")
        assert "SAST" in t.field_provenance["closing_at"]["note"]

    def test_repair_does_not_lower_confidence_or_trigger_review(self):
        """A deterministic, evidenced correction is MORE accurate, not less —
        it must not demote the date to "verify at source" (§10.3)."""
        t = self._repaired(closing_at="re-interpreted as SAST (+02:00)")
        assert t.confidence_for("closing_at") >= 0.80
        assert t.review_items == []

    def test_untouched_fields_stay_source(self):
        t = self._repaired(closing_at="tz repair")
        assert t.field_provenance["title"]["source"] == "SOURCE"
        assert "note" not in t.field_provenance["title"]

    def test_inferred_beats_derived_when_no_time_was_published(self):
        """A date-only closing is a GUESS even if we also re-zoned it."""
        t = normalize_notice(_notice(
            closing_at=datetime(2026, 9, 15, 0, 0, tzinfo=SAST),
            derived_fields={"closing_at": "tz repair"},
        ))
        assert t.field_provenance["closing_at"]["source"] == "INFERRED"
        assert t.confidence_for("closing_at") == 0.75

    def test_briefing_repair_recorded(self):
        t = normalize_notice(_notice(
            briefing_at=datetime(2026, 8, 25, 10, 30, tzinfo=SAST),
            compulsory_briefing=True,
            derived_fields={"briefing_at": "tz repair"},
        ))
        assert t.field_provenance["briefing_at"]["source"] == "DERIVED"
        assert t.field_provenance["compulsory_briefing"]["source"] == "SOURCE"


class TestEntityResolution:
    def test_aliases_collapse(self):
        """The §7 canonical example."""
        variants = [
            "eThekwini Municipality",
            "eThekwini Metropolitan Municipality",
            "City of eThekwini",
        ]
        keys = {normalize_org_name(v) for v in variants}
        assert keys == {"ethekwini"}

    def test_resolver(self):
        r = OrgResolver()
        r.add_alias("eThekwini Metropolitan Municipality", "org-678")
        assert r.resolve("City of eThekwini") == "org-678"
        assert r.resolve("Unknown Entity XYZ") is None
        assert r.unresolved == ["Unknown Entity XYZ"]


class TestDedupe:
    def test_fingerprint_stable_across_sources(self):
        a = normalize_notice(_notice(source_id="src-a"))
        b = normalize_notice(
            _notice(
                source_id="src-b",
                source_url="https://etenders.gov.za/t/9",
                tender_number="SCM45/2026",              # different formatting
                buyer_name="City of eThekwini",           # different alias
            )
        )
        assert fingerprint(a) == fingerprint(b)
        assert are_duplicates(a, b)

    def test_fuzzy_title_match(self):
        assert titles_similar(
            "Supply and Installation of CCTV System",
            "Supply & Installation of CCTV Systems at Durban facilities",
            threshold=0.4,
        )

    def test_different_tenders_not_duplicates(self):
        a = normalize_notice(_notice())
        b = normalize_notice(
            _notice(
                tender_number="SCM 099/2026",
                title="Grass Cutting and Verge Maintenance",
            )
        )
        assert not are_duplicates(a, b)

    def test_authority_merge_official_wins(self):
        """§8: eTender (100) beats aggregator (70); conflicts are logged."""
        official = normalize_notice(
            _notice(
                source_id="etenders-ocds",
                closing_at=datetime(2026, 9, 30, 11, 0, tzinfo=SAST),
            ),
            authority_score=100,
        )
        aggregator = normalize_notice(
            _notice(
                source_id="aggregator-x",
                source_url="https://aggregator.example/t/1",
                closing_at=datetime(2026, 9, 28, 11, 0, tzinfo=SAST),
                description="Extra scope details only the aggregator had.",
            ),
            authority_score=70,
        )
        merged = merge_tenders(aggregator, official)  # order must not matter
        assert merged.source_id == "etenders-ocds"
        assert merged.closing_at.day == 30            # official value prevails
        assert merged.description == "Extra scope details only the aggregator had."
        conflict_fields = {c["field"] for c in merged.raw["conflicts"]}
        assert "closing_at" in conflict_fields         # disagreement surfaced
        assert len(merged.source_urls) == 2            # attribution retained


class TestStatus:
    def _verified(self, closing):
        return normalize_notice(_notice(closing_at=closing), authority_score=100)

    def test_open(self):
        now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
        t = self._verified(datetime(2026, 9, 15, 11, 0, tzinfo=SAST))
        assert compute_status(t, now=now) == "OPEN"

    def test_closing_soon_72h(self):
        now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        t = self._verified(datetime(2026, 9, 15, 11, 0, tzinfo=SAST))
        assert compute_status(t, now=now) == "CLOSING_SOON"

    def test_closed(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        t = self._verified(datetime(2026, 9, 15, 11, 0, tzinfo=SAST))
        assert compute_status(t, now=now) == "CLOSED"

    def test_unverified_is_unknown_never_alerts(self):
        """§10.3: unverified closing date => UNKNOWN, not CLOSING_SOON."""
        t = normalize_notice(_notice(), structured=False)  # low confidence
        now = t.closing_at - timedelta(hours=1)
        assert compute_status(t, now=now) == "UNKNOWN"

    def test_no_date_is_unknown(self):
        t = normalize_notice(_notice(closing_at=None))
        assert compute_status(t) == "UNKNOWN"

    def test_cancelled_and_awarded_override(self):
        t = self._verified(datetime(2026, 9, 15, 11, 0, tzinfo=SAST))
        assert compute_status(t, cancelled=True) == "CANCELLED"
        assert compute_status(t, awarded=True) == "AWARDED"


class TestEndToEnd:
    def test_real_ocds_release_through_pipeline(self):
        """Live-captured release -> notice -> canonical tender -> status."""
        payload = json.loads(
            (FIXTURES / "ocds_api" / "real_release_2026-08-14.json").read_text()
        )
        notice = release_to_notice(payload["release"], "etenders-ocds", "https://x")
        tender = normalize_notice(notice, authority_score=100)

        assert tender.normalized_tender_number == "zntm1266w"
        assert tender.compulsory_briefing is True
        assert tender.confidence_for("closing_at") >= 0.9
        # P0 timezone fix: the portal said "11:00Z", which means 11:00 SAST.
        assert tender.closing_at == datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
        assert tender.closing_at.astimezone(SAST).hour == 11
        assert tender.field_provenance["closing_at"]["source"] == "DERIVED"

        now = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
        assert compute_status(tender, now=now) == "OPEN"
        assert fingerprint(tender)  # stable, non-empty
