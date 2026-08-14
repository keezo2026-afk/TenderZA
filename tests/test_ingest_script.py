"""Pure-function tests for scripts/ingest_ocds.py (no DB, no network)."""

import importlib.util
from datetime import date
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ingest_ocds", Path(__file__).parent.parent / "scripts" / "ingest_ocds.py"
)
ingest_ocds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest_ocds)


def test_month_windows_span():
    windows = list(ingest_ocds.month_windows("2026-06", end=date(2026, 8, 14)))
    assert windows == [
        ("2026-06-01", "2026-06-30"),
        ("2026-07-01", "2026-07-31"),
        ("2026-08-01", "2026-08-14"),
    ]


def test_month_windows_year_boundary():
    windows = list(ingest_ocds.month_windows("2025-11", end=date(2026, 1, 15)))
    assert windows == [
        ("2025-11-01", "2025-11-30"),
        ("2025-12-01", "2025-12-31"),
        ("2026-01-01", "2026-01-15"),
    ]


def test_month_windows_single_partial_month():
    windows = list(ingest_ocds.month_windows("2026-08", end=date(2026, 8, 14)))
    assert windows == [("2026-08-01", "2026-08-14")]
