"""Registry seed integrity tests (Blueprint §4, Phase 0 acceptance)."""

from tenderza.adapters import get_adapter
from tenderza.registry import load_seed_sources
from tenderza.registry.loader import seed_source_configs

VALID_TYPES = {
    "METRO", "DISTRICT", "LOCAL", "PROVINCE", "NATIONAL",
    "SOE", "PUBLIC_ENTITY", "UNIVERSITY_TVET", "AGGREGATOR",
}

PROVINCES = {
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "North West", "Northern Cape", "Western Cape",
}


def test_seed_loads():
    rows = load_seed_sources()
    assert len(rows) >= 19  # eTender x2 + 9 provinces + 8 metros + GTB


def test_refs_unique():
    rows = load_seed_sources()
    refs = [r["ref"] for r in rows]
    assert len(refs) == len(set(refs))


def test_types_and_scores_valid():
    for row in load_seed_sources():
        assert row["type"] in VALID_TYPES, row["ref"]
        assert 0 <= row["authority_score"] <= 100, row["ref"]
        # §8: official sources score >= 90
        if row["type"] in {"NATIONAL", "PROVINCE"}:
            assert row["authority_score"] == 100, row["ref"]
        if row["type"] == "METRO":
            assert row["authority_score"] == 95, row["ref"]


def test_all_nine_provinces_present():
    provinces = {r["province"] for r in load_seed_sources() if r["type"] == "PROVINCE"}
    assert provinces == PROVINCES


def test_all_eight_metros_present():
    metros = [r for r in load_seed_sources() if r["type"] == "METRO"]
    assert len(metros) == 8  # blueprint §3.3


def test_every_seed_row_resolves_to_a_registered_adapter():
    for config in seed_source_configs():
        adapter = get_adapter(config)
        assert adapter.config.source_id == config.source_id
