"""Tender-number normalization tests against the golden fixture corpus (§7)."""

import json
from pathlib import Path

import pytest

from tenderza.normalize import normalize_tender_number

FIXTURES = Path(__file__).parent / "fixtures" / "tender_numbers.json"


def _cases():
    payload = json.loads(FIXTURES.read_text())
    return [(c["raw"], c["expected"]) for c in payload["cases"]]


@pytest.mark.parametrize("raw,expected", _cases())
def test_corpus(raw, expected):
    assert normalize_tender_number(raw) == expected


def test_equivalence_groups_collapse():
    """The dedupe property itself: known-equivalent raws share one canonical form."""
    groups = [
        ["SCM 045/2026", "SCM45/2026", "scm-045-2026", "SCM/045/2026"],
        ["1H-13541", "1H 13541"],
        ["  WCG/TR 019/26  ", "WCG-TR-19/26"],
    ]
    for group in groups:
        canon = {normalize_tender_number(raw) for raw in group}
        assert len(canon) == 1, f"group did not collapse: {group} -> {canon}"


def test_pure_and_idempotent():
    value = "SCM 045/2026"
    once = normalize_tender_number(value)
    assert normalize_tender_number(once) == once
