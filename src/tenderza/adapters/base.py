"""Generic adapter framework (Blueprint §5).

Contract
--------
* An **Adapter** is one codebase configured per source (§5.2): it fetches a
  source's listings and yields ``RawTenderNotice`` items plus document
  references. It never writes to the database itself — persistence is the
  pipeline's job — and it never uses AI to decide what to crawl (§6 doctrine).
* Every adapter ships with recorded sample pages (golden fixtures) and
  expected output under ``tests/fixtures/adapters/<adapter>/`` — this is the
  knowledge-loss mitigation of §19.
* Politeness (§5.3): adapters must honour ``SourceConfig.rate_limit_per_min``,
  send the project user agent, use conditional fetching (ETag/Last-Modified)
  where the transport layer supports it, and NEVER bypass WAFs/CAPTCHAs.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

USER_AGENT = (
    "TenderZA-Crawler/0.1 (+https://github.com/keezo2026-afk/TenderZA; "
    "polite crawler; contact: ops@tenderza.example)"
)


@dataclass(frozen=True)
class SourceConfig:
    """Per-source configuration handed to an adapter (registry row, §4)."""

    source_id: str
    name: str
    crawl_url: str
    adapter: str                          # adapter key, e.g. "ocds_api", "generic_cms"
    org_type: str = "UNKNOWN"             # Metro / Province / SOE / ...
    province: str | None = None
    authority_score: int = 50             # §8
    triage_tier: int | None = None        # 1-4; None while in discovery
    rate_limit_per_min: int = 12          # §5.3 politeness default
    obey_robots_txt: bool = True          # non-negotiable for HTML adapters (§17.1)
    conditional_fetch: bool = True        # ETag / Last-Modified (§5.3)
    options: dict[str, Any] = field(default_factory=dict)  # adapter-specific knobs


@dataclass
class DocumentRef:
    """A linked tender document to be fetched into object storage by hash (§5.1)."""

    url: str
    filename: str | None = None


@dataclass
class RawTenderNotice:
    """One tender notice as seen at the source — pre-normalization.

    Field values are exactly what the source published; the Normalizer and
    the extraction pipeline attach provenance/confidence downstream (§6).
    """

    source_id: str
    source_url: str                        # page the notice was found on
    title: str
    tender_number: str | None = None
    buyer_name: str | None = None          # resolved to an organisation later (§7)
    description: str | None = None
    province: str | None = None
    categories: list[str] = field(default_factory=list)
    published_at: datetime | None = None   # tz-aware or None — never naive (§10.2.1)
    closing_at: datetime | None = None
    briefing_at: datetime | None = None
    compulsory_briefing: bool | None = None
    documents: list[DocumentRef] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # verbatim source payload
    # Fields the adapter had to repair because the publisher's own data was
    # wrong (e.g. eTenders stamping SAST wall times with a "Z"). Maps field
    # name -> human-readable reason; the Normalizer turns these into DERIVED
    # provenance so the correction is disclosed, never silent (§10.2.5).
    derived_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class AdapterResult:
    """Outcome of one adapter run (feeds crawl_results, append-only §5.3)."""

    notices: list[RawTenderNotice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    not_modified: int = 0                  # conditional-fetch hits


class Adapter(abc.ABC):
    """Base class for all source adapters."""

    #: unique registry key, e.g. "ocds_api", "generic_cms", "generic_sitemap_rss"
    key: str = ""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @abc.abstractmethod
    def fetch(self) -> Iterator[RawTenderNotice]:
        """Yield raw tender notices from the source. Deterministic, polite."""

    def run(self) -> AdapterResult:
        """Collect ``fetch()`` output into an AdapterResult, capturing errors."""
        result = AdapterResult()
        try:
            for notice in self.fetch():
                result.notices.append(notice)
        except Exception as exc:  # noqa: BLE001 — surfaced to crawl_jobs/backoff
            result.errors.append(f"{type(exc).__name__}: {exc}")
        return result


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[Adapter]] = {}


def register_adapter(cls: type[Adapter]) -> type[Adapter]:
    """Class decorator: register an adapter under its ``key``."""
    if not cls.key:
        raise ValueError(f"{cls.__name__} must define a non-empty 'key'")
    if cls.key in _REGISTRY:
        raise ValueError(f"duplicate adapter key: {cls.key!r}")
    _REGISTRY[cls.key] = cls
    return cls


def get_adapter(config: SourceConfig) -> Adapter:
    """Instantiate the adapter named by ``config.adapter``."""
    try:
        cls = _REGISTRY[config.adapter]
    except KeyError as exc:
        raise KeyError(
            f"no adapter registered for {config.adapter!r}; "
            f"known: {sorted(_REGISTRY)}"
        ) from exc
    return cls(config)


def known_adapters() -> list[str]:
    return sorted(_REGISTRY)
