"""eTender Transparency Portal OCDS API ingestor (Blueprint §3.1, §5.1).

Phase 0 note (§3.1): the exact API base URL, pagination scheme and rate
limits MUST be verified against the portal's published OpenAPI spec at
https://data.etenders.gov.za/ before production use. The default below is
the community-documented endpoint; override via SourceConfig.options:

    options = {
        "base_url": "https://ocds-api.etenders.gov.za",
        "releases_path": "/api/OCDSReleases",
        "page_size": 50,
        "date_from": "2021-05-01",   # historical backfill start (§3.1)
        "date_to": None,
    }

Doctrine: raw releases are archived VERBATIM into ocds_records (the pipeline
does this from RawTenderNotice.raw); canonical tenders are derived from them
(§10.2.6). Data is CC BY 4.0 — attribution is preserved via source_url.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from tenderza.adapters.base import (
    USER_AGENT,
    Adapter,
    DocumentRef,
    RawTenderNotice,
    register_adapter,
)

DEFAULT_BASE_URL = "https://ocds-api.etenders.gov.za"
DEFAULT_RELEASES_PATH = "/api/OCDSReleases"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def release_to_notice(release: dict[str, Any], source_id: str, source_url: str) -> RawTenderNotice:
    """Map one OCDS release to a RawTenderNotice (pure — unit-testable)."""
    tender = release.get("tender") or {}
    buyer = release.get("buyer") or {}
    period = tender.get("tenderPeriod") or {}

    documents = [
        DocumentRef(url=doc["url"], filename=doc.get("title"))
        for doc in tender.get("documents") or []
        if doc.get("url")
    ]

    return RawTenderNotice(
        source_id=source_id,
        source_url=source_url,
        title=tender.get("title") or release.get("ocid", "untitled"),
        tender_number=tender.get("id"),
        buyer_name=buyer.get("name"),
        description=tender.get("description"),
        published_at=_parse_dt(release.get("date")),
        closing_at=_parse_dt(period.get("endDate")),
        documents=documents,
        raw=release,  # archived verbatim into ocds_records
    )


@register_adapter
class OcdsApiAdapter(Adapter):
    """Polls the Treasury OCDS REST API; supports historical backfill."""

    key = "ocds_api"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )

    def fetch(self) -> Iterator[RawTenderNotice]:
        opts = self.config.options
        base_url = (opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        path = opts.get("releases_path") or DEFAULT_RELEASES_PATH
        page_size = int(opts.get("page_size") or 50)
        max_pages = int(opts.get("max_pages") or 0)  # 0 = no cap

        page = 1
        with self._client() as client:
            while True:
                params: dict[str, Any] = {"PageNumber": page, "PageSize": page_size}
                if opts.get("date_from"):
                    params["dateFrom"] = opts["date_from"]
                if opts.get("date_to"):
                    params["dateTo"] = opts["date_to"]

                url = f"{base_url}{path}"
                resp = client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()

                releases = payload.get("releases") or []
                if not releases:
                    return
                for release in releases:
                    yield release_to_notice(
                        release,
                        source_id=self.config.source_id,
                        source_url=str(resp.url),
                    )

                page += 1
                if max_pages and page > max_pages:
                    return

    def fetch_openapi_spec(self, spec_url: str | None = None) -> dict[str, Any]:
        """Phase 0 verification helper: pull the OpenAPI spec to confirm
        the real endpoint paths, parameters and limits (§3.1)."""
        url = spec_url or self.config.options.get(
            "openapi_url",
            f"{(self.config.options.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}"
            "/swagger/v1/swagger.json",
        )
        with self._client() as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
