"""Source Registry seed loader (Blueprint §4).

Reads seed_sources.json into SourceConfig objects for the scheduler, and
provides a psycopg-based upsert for the `sources` table (used by
scripts/seed_registry.py once a database is available).
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from tenderza.adapters.base import SourceConfig

_TIER_DEFAULT_FREQ_MIN = {1: 30, 2: 90, 3: 300, 4: 1440}  # §5.3 tiers


def load_seed_sources() -> list[dict[str, Any]]:
    """Return the raw seed rows from seed_sources.json."""
    payload = json.loads(
        resources.files("tenderza.registry").joinpath("seed_sources.json").read_text()
    )
    return payload["sources"]


def seed_source_configs() -> list[SourceConfig]:
    """Seed rows as SourceConfig objects ready for get_adapter()."""
    configs: list[SourceConfig] = []
    for row in load_seed_sources():
        tier = row.get("triage_tier")
        configs.append(
            SourceConfig(
                source_id=row["ref"],
                name=row["name"],
                crawl_url=row.get("tender_url") or row.get("website") or "",
                adapter=row.get("adapter") or "generic_cms",
                org_type=row.get("type", "UNKNOWN"),
                province=row.get("province"),
                authority_score=row.get("authority_score", 50),
                triage_tier=tier,
                options={
                    "platform": row.get("platform"),
                    "frequency_min": row.get("frequency_min")
                    or _TIER_DEFAULT_FREQ_MIN.get(tier or 4, 1440),
                },
            )
        )
    return configs
