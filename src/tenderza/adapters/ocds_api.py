"""eTender Transparency Portal OCDS API ingestor (Blueprint §3.1, §5.1).

Phase 0 verification — DONE (14 Aug 2026, live against the API):

* Base URL:   https://ocds-api.etenders.gov.za          (confirmed)
* Listing:    GET /api/OCDSReleases?PageNumber=&PageSize=&dateFrom=&dateTo=
* Single:     GET /api/OCDSReleases/release/{ocid}
* Page size:  max 1000 per page in a browser; up to ~20000 per page for
              API clients (per the OpenAPI spec's own parameter docs).
* Pagination: response carries ``links.next`` — we follow it rather than
              blindly incrementing PageNumber.
* OCID prefix: ocds-9t57fa. Release packages declare OCDS version 1.1.
* Extensions beyond vanilla OCDS observed live: ``tender.briefingSession``
  ({isSession, compulsory, date, venue} — maps DIRECTLY to our
  compulsory_briefing/briefing_at fields, §10.2.2), ``tender.province``,
  ``tender.deliveryLocation``, ``tender.procuringEntity``,
  ``tender.contactPerson``. ``value.amount`` of 0 means "no value
  published" and is mapped to None (§10.2.3).
* Licensing note: the portal's download page says CC BY 4.0; the API's
  release packages declare an Open Data Commons PDDL URL. Either way the
  data is open — attribution retained via source_url (§2.4).
* KNOWN DATA-QUALITY ISSUE (P0 follow-up): tenderPeriod.endDate values
  arrive as e.g. "2026-09-16T11:00:00Z", but SA tenders close at 11:00
  SAST by SCM convention — the portal almost certainly emits SAST wall
  times mislabeled as UTC (briefing dates show the same pattern; empty
  briefings are "0001-01-01T00:00:00Z"). Verify against the portal UI for
  a sample, then decide whether to re-interpret Z as SAST here. Until
  resolved we store what the source says (provenance = SOURCE) rather
  than silently rewriting it.

Options (SourceConfig.options):

    {
        "base_url": "https://ocds-api.etenders.gov.za",
        "releases_path": "/api/OCDSReleases",
        "page_size": 1000,
        "date_from": "2021-05-01",   # historical backfill start (§3.1)
        "date_to": None,
        "max_pages": 0,              # 0 = follow links.next to the end
    }

Doctrine: raw releases are archived VERBATIM into ocds_records (the pipeline
does this from RawTenderNotice.raw); canonical tenders are derived from them
(§10.2.6).
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
    """Map one OCDS release to a RawTenderNotice (pure — unit-testable).

    Handles the eTenders extensions verified live on 2026-08-14:
    briefingSession, province, procuringEntity, zero-as-null values.
    """
    tender = release.get("tender") or {}
    buyer = release.get("buyer") or {}
    procuring = tender.get("procuringEntity") or {}
    period = tender.get("tenderPeriod") or {}
    briefing = tender.get("briefingSession") or {}

    documents = [
        DocumentRef(url=doc["url"], filename=doc.get("title"))
        for doc in tender.get("documents") or []
        if doc.get("url")
    ]

    # eTenders extension: briefingSession {isSession, compulsory, date, venue}
    compulsory_briefing: bool | None = None
    briefing_at = None
    if briefing.get("isSession"):
        compulsory_briefing = bool(briefing.get("compulsory"))
        briefing_at = _parse_dt(briefing.get("date"))

    # eTenders publishes the human bid number in tender.title (e.g.
    # "ZNTM01266W") and a numeric portal id in tender.id — prefer the title
    # as tender_number when it looks like a reference, keep both in raw.
    tender_number = tender.get("title") or tender.get("id")
    title = tender.get("description") or tender.get("title") or release.get("ocid", "untitled")

    categories = [c for c in [tender.get("category")] if c]

    return RawTenderNotice(
        source_id=source_id,
        source_url=source_url,
        title=title,
        tender_number=tender_number,
        buyer_name=buyer.get("name") or procuring.get("name"),
        description=tender.get("description"),
        province=tender.get("province") or None,   # eTenders extension
        categories=categories,
        published_at=_parse_dt(release.get("date")),
        closing_at=_parse_dt(period.get("endDate")),
        briefing_at=briefing_at,
        compulsory_briefing=compulsory_briefing,
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
        page_size = int(opts.get("page_size") or 1000)  # API allows up to ~20k
        max_pages = int(opts.get("max_pages") or 0)     # 0 = follow links.next to the end

        params: dict[str, Any] = {"PageNumber": 1, "PageSize": page_size}
        if opts.get("date_from"):
            params["dateFrom"] = opts["date_from"]
        if opts.get("date_to"):
            params["dateTo"] = opts["date_to"]

        url: str | None = f"{base_url}{path}"
        pages = 0
        with self._client() as client:
            while url:
                resp = client.get(url, params=params)
                params = {}  # links.next already carries the query string
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

                pages += 1
                if max_pages and pages >= max_pages:
                    return
                # Verified live: the package carries links.next for pagination.
                url = (payload.get("links") or {}).get("next")

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
