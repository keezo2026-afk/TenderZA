"""Change-detection tests (Blueprint §9): field diffs + change classification."""

from datetime import datetime, timezone

from tenderza.persistence.diff import classify_change, diff_tender_fields

UTC = timezone.utc


def test_no_changes():
    fields = {"title": "T", "closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC)}
    assert diff_tender_fields(fields, dict(fields)) == {}
    assert classify_change({}) is None


def test_extension_detected():
    old = {"closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC)}
    new = {"closing_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC)}
    changes = diff_tender_fields(old, new)
    assert "closing_at" in changes
    assert classify_change(changes) == "EXTENDED"


def test_shortening_detected():
    old = {"closing_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC)}
    new = {"closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC)}
    assert classify_change(diff_tender_fields(old, new)) == "SHORTENED"


def test_cancellation_beats_other_changes():
    changes = diff_tender_fields(
        {"status": "OPEN", "title": "A"},
        {"status": "CANCELLED", "title": "B"},
    )
    assert classify_change(changes) == "CANCELLED"


def test_absent_new_value_is_not_a_change():
    """A source temporarily omitting a field must not erase history (§9)."""
    old = {"closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC), "title": "T"}
    new = {"closing_at": None, "title": "T"}
    assert diff_tender_fields(old, new) == {}


def test_detail_change():
    changes = diff_tender_fields({"description": "old"}, {"description": "new"})
    assert classify_change(changes) == "DETAILS_CHANGED"


def test_datetimes_serialized_json_safe():
    old = {"closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC)}
    new = {"closing_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC)}
    changes = diff_tender_fields(old, new)
    assert isinstance(changes["closing_at"]["old"], str)  # ISO strings for jsonb
    assert isinstance(changes["closing_at"]["new"], str)
