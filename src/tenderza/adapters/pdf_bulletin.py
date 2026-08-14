"""Generic PDF-bulletin adapter (Blueprint §5.2.2, third generic adapter).

Fetches a source's single "tenders" PDF, computes its content hash, and —
only when the hash changed since the last crawl — emits a notice carrying
the document reference for the Document Pipeline (§6) to parse.

Skeleton status: hash-diff logic implemented; previous-hash lookup is
injected via options["previous_hash"] until the crawl_results-backed store
lands in Phase 1.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import httpx

from tenderza.adapters.base import (
    USER_AGENT,
    Adapter,
    DocumentRef,
    RawTenderNotice,
    register_adapter,
)


def content_hash(data: bytes) -> str:
    """SHA-256 content hash — the object-storage key basis (§5.1)."""
    return hashlib.sha256(data).hexdigest()


@register_adapter
class PdfBulletinAdapter(Adapter):
    key = "pdf_bulletin"

    def fetch(self) -> Iterator[RawTenderNotice]:
        opts = self.config.options
        pdf_url = opts.get("pdf_url") or self.config.crawl_url
        previous_hash = opts.get("previous_hash")

        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
        ) as client:
            resp = client.get(pdf_url)
            resp.raise_for_status()
            digest = content_hash(resp.content)

        if previous_hash and digest == previous_hash:
            return  # unchanged bulletin — nothing to emit (conditional-fetch spirit)

        yield RawTenderNotice(
            source_id=self.config.source_id,
            source_url=pdf_url,
            title=f"{self.config.name} tender bulletin",
            documents=[DocumentRef(url=pdf_url, filename=pdf_url.rsplit("/", 1)[-1])],
            raw={"content_hash": digest, "format": "pdf_bulletin"},
        )
