"""Document pipeline unit tests: storage, text extraction, field extraction."""

from datetime import datetime

import pytest

from tenderza.documents.extract import extract_fields
from tenderza.documents.storage import ObjectStore, content_hash, object_key
from tenderza.documents.text import extract_text

# ---------------------------------------------------------------------------
# Realistic SA tender-document text (mirrors common phrasing conventions)
# ---------------------------------------------------------------------------

BULLETIN_TEXT = """
ETHEKWINI MUNICIPALITY — SUPPLY CHAIN MANAGEMENT UNIT
INVITATION TO TENDER

Tender No: 1H-13541
Description: Construction of a new community hall at Ward 62, including
civil works, electrical installation and 12-month defects liability.

CIDB Requirement: Tenderers must have a CIDB contractor grading of 6GB or
higher. Joint ventures 5GB PE accepted.

B-BBEE Status Level: Level 2 contributor minimum.

A COMPULSORY site briefing will be held on 25 August 2026 at 10h00 at the
Ward 62 Community Centre, Umlazi.

Closing Date: 15 September 2026 at 11h00.
Late submissions will not be accepted.

Estimated value: R 4 500 000.00 (incl. VAT)

Enquiries: Mr S. Dlamini, scm.enquiries@durban.gov.za, 031 311 7645.
"""

RFQ_TEXT = """
REQUEST FOR QUOTATION
RFQ Number: MLM/RFQ/2026/113
Supply and delivery of 200 refuse bins.
Quotations must be submitted by e-mail.
Closing date: 2026-08-28
Contact: procurement@mocklocal.gov.za / +27 35 901 2200
"""

NO_TIME_TEXT = """
BID NO. SCM 077/2026
Closing date: 30 September 2026.
A non-compulsory briefing session takes place on 5 September 2026 at 14h00.
"""


class TestFieldExtraction:
    def test_bulletin_full_extraction(self):
        f = extract_fields(BULLETIN_TEXT)
        assert f.tender_number.value == "1H-13541"
        assert f.tender_number.confidence >= 0.8

        assert f.closing_at.value == datetime.fromisoformat(
            "2026-09-15T11:00:00+02:00")
        assert f.closing_at.confidence >= 0.7      # explicit time present

        assert f.compulsory_briefing.value is True
        assert f.briefing_at.value.day == 25
        assert f.briefing_at.value.hour == 10

        assert f.cidb_grades.value == ["5GB", "6GB"]
        assert f.bbee_level.value == "Level 2"
        assert f.contact_email.value == "scm.enquiries@durban.gov.za"
        assert f.contact_phone.value == "0313117645"
        assert f.value_estimated.value == 4_500_000.0

    def test_rfq_numeric_date_no_time_gets_scm_convention(self):
        f = extract_fields(RFQ_TEXT)
        assert f.tender_number.value == "MLM/RFQ/2026/113"
        dt = f.closing_at.value
        assert (dt.year, dt.month, dt.day) == (2026, 8, 28)
        assert (dt.hour, dt.minute) == (11, 0)         # SCM 11:00 convention
        assert f.closing_at.confidence < 0.7           # no explicit time
        assert f.contact_email.value == "procurement@mocklocal.gov.za"

    def test_non_compulsory_briefing_not_flagged(self):
        f = extract_fields(NO_TIME_TEXT)
        assert f.tender_number.value == "SCM 077/2026"
        assert f.briefing_at is not None
        # "non-compulsory information session": must NOT set compulsory=True
        assert f.compulsory_briefing is None or f.compulsory_briefing.value is False

    def test_cidb_requires_context(self):
        # Grade-like tokens without CIDB context must not match (product codes)
        f = extract_fields("Supply of 6GB RAM modules and 9CE connectors.")
        assert f.cidb_grades is None

    def test_value_sanity_band(self):
        f = extract_fields("Estimated value: R 12.50")   # too small — reject
        assert f.value_estimated is None

    def test_empty_text(self):
        f = extract_fields("")
        assert f.warnings == ["empty text"]
        assert f.as_dict() == {}

    def test_evidence_snippets_present(self):
        f = extract_fields(BULLETIN_TEXT)
        d = f.as_dict()
        assert "Closing Date" in d["closing_at"]["evidence"]
        assert all("confidence" in v for v in d.values())

    def test_document_confidence_capped_below_structured(self):
        """PDF-extracted closing dates must sit below the 0.98 given to
        structured feeds — the portal must win authority merges (§8)."""
        f = extract_fields(BULLETIN_TEXT)
        assert f.closing_at.confidence < 0.98


class TestObjectStore:
    def test_put_get_roundtrip(self, tmp_path):
        store = ObjectStore(tmp_path)
        obj = store.put(b"tender bulletin bytes", suffix=".pdf")
        assert not obj.already_existed
        assert store.get(obj.key) == b"tender bulletin bytes"
        assert obj.key == object_key(content_hash(b"tender bulletin bytes"), ".pdf")
        assert obj.key.startswith("docs/")

    def test_same_bytes_stored_once(self, tmp_path):
        store = ObjectStore(tmp_path)
        a = store.put(b"same bulletin", suffix=".pdf")
        b = store.put(b"same bulletin", suffix=".pdf")
        assert b.already_existed
        assert a.key == b.key                      # §5.1: one object, two pages

    def test_text_cache(self, tmp_path):
        store = ObjectStore(tmp_path)
        obj = store.put(b"doc")
        assert store.get_text(obj.digest) is None
        store.put_text(obj.digest, "extracted words")
        assert store.get_text(obj.digest) == "extracted words"


class TestPdfTextExtraction:
    @pytest.fixture()
    def pdf_bytes(self) -> bytes:
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Tender No: 1H-13541")
        page.insert_text((72, 130), "Closing Date: 15 September 2026 at 11h00")
        data = doc.tobytes()
        doc.close()
        return data

    def test_text_pdf_extracts(self, pdf_bytes):
        et = extract_text(pdf_bytes, filename="bulletin.pdf")
        assert et.usable and et.pages == 1
        assert "1H-13541" in et.text
        fields = extract_fields(et.text)
        assert fields.tender_number.value == "1H-13541"

    def test_scanned_pdf_flagged_for_ocr(self):
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        doc.new_page()                       # empty page = textless
        data = doc.tobytes()
        doc.close()
        et = extract_text(data)
        assert et.needs_ocr and not et.usable
        assert any("OCR" in w for w in et.warnings)

    def test_corrupt_pdf_reported(self):
        et = extract_text(b"%PDF-1.4 garbage")
        assert not et.usable
        assert et.warnings

    def test_unknown_type_reported(self):
        et = extract_text(b"\x00\x01binary", filename="notes.xyz")
        assert et.kind == "unknown"
        assert not et.usable
