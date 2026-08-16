"""Document processor integration tests — real Postgres, no network
(fetch_document monkeypatched). Skipped without TEST_DATABASE_URL."""

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
pymupdf = pytest.importorskip("pymupdf")

from tenderza.adapters.base import DocumentRef, RawTenderNotice  # noqa: E402
from tenderza.documents import processor  # noqa: E402
from tenderza.documents.storage import ObjectStore  # noqa: E402
from tenderza.persistence import TenderStore  # noqa: E402
from tenderza.pipeline import normalize_notice  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)


def _pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 100
    for line in lines:
        page.insert_text((72, y), line)
        y += 24
    data = doc.tobytes()
    doc.close()
    return data


BULLETIN_PDF = _pdf([
    "MOCK LOCAL MUNICIPALITY — TENDER NOTICE",
    "Tender No: MLM 012/2026",
    "Rehabilitation of Gravel Roads Ward 4.",
    "A COMPULSORY site briefing will be held on 21 August 2026 at 10h00.",
    "Closing Date: 19 September 2026 at 11h00.",
    "CIDB grading of 5CE or higher required.",
    "Enquiries: scm@mocklocal.gov.za",
])


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


@pytest.fixture()
def store(tmp_path):
    return ObjectStore(tmp_path)


@pytest.fixture()
def tender_with_doc(conn):
    """A sitemap-stub tender (unverified, no closing date) with one PDF doc —
    the exact shape the crawler produces for PDF-only municipal sources.

    Unique per test: process_pending() commits (production behaviour), so
    fixture rollbacks cannot isolate tests sharing one tender.
    """
    import uuid
    uniq = uuid.uuid4().hex[:8]
    notice = RawTenderNotice(
        source_id="src-mock",
        source_url=f"https://mocklocal.gov.za/tenders/{uniq}",
        title=f"mock tender {uniq}",
        tender_number=f"MOCK {uniq}",
        buyer_name=f"Mock Municipality {uniq}",
        documents=[DocumentRef(url=f"https://mocklocal.gov.za/docs/{uniq}.pdf",
                               filename=f"{uniq}.pdf")],
        raw={"format": "sitemap"},
    )
    tender = normalize_notice(notice, authority_score=95, structured=False)
    result = TenderStore(conn).upsert_tender(tender)
    conn.commit()
    return result.tender_id


def _run(conn, store, monkeypatch, tender_id, pdf_bytes=BULLETIN_PDF):
    """Process pending docs; return outcomes for OUR tender only (commits
    from prior tests leave their docs in the shared DB)."""
    monkeypatch.setattr(processor, "fetch_document", lambda url, **kw: pdf_bytes)
    outcomes = processor.process_pending(conn, store, limit=50)
    return [o for o in outcomes if o.tender_id == tender_id]


class TestProcessing:
    def test_document_enriches_bare_tender(self, conn, store, monkeypatch,
                                           tender_with_doc):
        outcomes = _run(conn, store, monkeypatch, tender_with_doc)
        assert len(outcomes) == 1
        o = outcomes[0]
        assert o.stored and not o.error
        assert o.fields_found >= 5
        assert "closing_at" in o.enriched          # tender had no date at all
        assert "compulsory_briefing" in o.enriched

        with conn.cursor() as cur:
            cur.execute(
                "SELECT closing_at, compulsory_briefing, field_provenance "
                "FROM tenders WHERE id = %s", (tender_with_doc,),
            )
            closing_at, compulsory, prov = cur.fetchone()
        assert closing_at == datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)  # 11:00 SAST
        assert compulsory is True
        assert prov["closing_at"]["source"] == "DERIVED"
        assert prov["closing_at"]["source_id"].startswith("doc:")

    def test_low_confidence_high_stakes_queued_for_review(
            self, conn, store, monkeypatch, tender_with_doc):
        outcomes = _run(conn, store, monkeypatch, tender_with_doc)
        assert "closing_at" in outcomes[0].queued_for_review  # 0.75 < 0.80
        with conn.cursor() as cur:
            cur.execute(
                "SELECT field, value FROM review_queue "
                "WHERE tender_id = %s AND resolved_at IS NULL", (tender_with_doc,),
            )
            rows = dict(cur.fetchall())
        assert "closing_at" in rows
        assert "evidence" in rows["closing_at"]      # snippet for the reviewer

    def test_document_never_overrides_higher_confidence(
            self, conn, store, monkeypatch, tender_with_doc):
        """§8: a 0.75-confidence PDF date must not beat a 0.98 portal date."""
        portal_date = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenders SET closing_at = %s,
                    field_provenance = field_provenance ||
                    '{"closing_at": {"source": "SOURCE", "source_id":
                      "etenders-ocds", "confidence": 0.98}}'::jsonb
                WHERE id = %s
                """,
                (portal_date, tender_with_doc),
            )
        outcomes = _run(conn, store, monkeypatch, tender_with_doc)
        assert "closing_at" not in (outcomes[0].enriched or [])
        with conn.cursor() as cur:
            cur.execute("SELECT closing_at FROM tenders WHERE id = %s",
                        (tender_with_doc,))
            assert cur.fetchone()[0] == portal_date   # portal value survived

    def test_duplicate_bytes_use_cached_text(self, conn, store, monkeypatch,
                                             tender_with_doc):
        _run(conn, store, monkeypatch, tender_with_doc)
        # Second doc row pointing at identical bytes (bulletin on two pages)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tender_documents (tender_id, doc_url, filename) "
                "VALUES (%s, 'https://other.page/same.pdf', 'same.pdf')",
                (tender_with_doc,),
            )
        conn.commit()
        outcomes = _run(conn, store, monkeypatch, tender_with_doc)
        assert outcomes[0].deduped                    # content-hash hit (§5.1)

    def test_scanned_pdf_flagged_not_faked(self, conn, store, monkeypatch,
                                           tender_with_doc):
        doc = pymupdf.open()
        doc.new_page()                                # textless
        scanned = doc.tobytes()
        doc.close()
        outcomes = _run(conn, store, monkeypatch, tender_with_doc, pdf_bytes=scanned)
        o = outcomes[0]
        assert o.needs_ocr and o.fields_found == 0
        assert not o.enriched                          # nothing invented
