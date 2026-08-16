"""Alert unit tests: matcher SQL building + digest rendering (no DB)."""

from datetime import datetime, timedelta, timezone

from tenderza.alerts.emailer import render_digest, send_email
from tenderza.alerts.matcher import ALERTABLE_STATUSES, build_match_query

SAST = timezone(timedelta(hours=2))


def _alert(**kw):
    base = {"id": "a-1", "keywords": "", "provinces": [], "categories": [],
            "batch_prefs": {}}
    base.update(kw)
    return base


class TestMatcherQuery:
    def test_placeholders_match_params(self):
        for alert in (
            _alert(),
            _alert(keywords="cctv security"),
            _alert(provinces=["KwaZulu-Natal", "Gauteng"]),
            _alert(categories=["construction"]),
            _alert(keywords="roads", provinces=["Free State"],
                   batch_prefs={"closing_within_days": 14}),
        ):
            sql, params = build_match_query(alert)
            assert sql.count("%s") == len(params), alert

    def test_at_most_once_guard_present(self):
        sql, _ = build_match_query(_alert())
        assert "NOT EXISTS" in sql and "alert_events" in sql

    def test_closed_never_alertable(self):
        assert "CLOSED" not in ALERTABLE_STATUSES
        assert "CANCELLED" not in ALERTABLE_STATUSES

    def test_deadline_filter_requires_verified_date(self):
        sql, _ = build_match_query(_alert(batch_prefs={"closing_within_days": 7}))
        assert "confidence" in sql          # §14: unverified dates never drive
        assert "0.80" in sql                # deadline alerts

    def test_reads_only_normalized_table(self):
        sql, _ = build_match_query(_alert(keywords="x"))
        assert "crawl_results" not in sql
        assert "FROM tenders" in sql


class TestDigestRendering:
    def _tender(self, **kw):
        base = {
            "id": "t-1", "tender_number": "SCM 045/2026",
            "title": "Supply and Installation of CCTV",
            "buyer_name": "eThekwini Metropolitan Municipality",
            "province": "KwaZulu-Natal",
            "closing_at": datetime(2026, 9, 15, 11, 0, tzinfo=SAST),
            "compulsory_briefing": False,
            "original_url": "https://durban.gov.za/t/1",
            "field_provenance": {
                "closing_at": {"source": "SOURCE", "confidence": 0.98}
            },
        }
        base.update(kw)
        return base

    def test_verified_date_shown(self):
        subject, body = render_digest(_alert(keywords="cctv"), [self._tender()])
        assert "1 new tender" in subject
        assert "15 Sep 2026, 11:00 SAST" in body
        assert "UNVERIFIED" not in body

    def test_unverified_date_flagged_never_shown_as_fact(self):
        t = self._tender(field_provenance={
            "closing_at": {"source": "INFERRED", "confidence": 0.6}})
        _, body = render_digest(_alert(), [t])
        assert "UNVERIFIED" in body
        assert "verify at source" in body
        assert "15 Sep 2026" not in body     # the date itself is withheld

    def test_compulsory_briefing_warning(self):
        _, body = render_digest(_alert(), [self._tender(compulsory_briefing=True)])
        assert "COMPULSORY briefing" in body

    def test_links_and_attribution_no_attachments(self):
        _, body = render_digest(_alert(), [self._tender()],
                                base_url="https://tenderza.example")
        assert "https://tenderza.example/tender/t-1" in body
        assert "CC BY 4.0" in body

    def test_plural_subject(self):
        subject, _ = render_digest(_alert(keywords="roads"),
                                   [self._tender(), self._tender(id="t-2")])
        assert "2 new tenders" in subject


class TestDevOutbox:
    def test_send_email_writes_eml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.setenv("ALERT_OUTBOX", str(tmp_path))
        ref = send_email("user@example.co.za", "Test subject", "Body text")
        assert ref.startswith("outbox:")
        files = list(tmp_path.glob("*.eml"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "Test subject" in content and "user@example.co.za" in content
