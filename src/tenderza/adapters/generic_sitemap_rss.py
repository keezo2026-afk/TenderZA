"""Generic sitemap/RSS/Atom adapter (Blueprint §5.2.2).

Pure sitemap.xml / RSS / Atom ingestion — covers a large share of small
municipal sites with zero per-site code. Configure per source:

    options = {
        "feed_url": "https://example.gov.za/tenders/feed",   # RSS/Atom, or
        "sitemap_url": "https://example.gov.za/sitemap.xml",
        "url_filter": "tender",   # substring filter for sitemap URLs
    }

Skeleton status: RSS/Atom parsing implemented against stdlib only; listing
pages discovered via sitemap are emitted as bare notices for the document
pipeline to enrich. Golden fixtures in tests/fixtures/adapters/.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from tenderza.adapters.base import (
    USER_AGENT,
    Adapter,
    RawTenderNotice,
    register_adapter,
)

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
}


def _parse_feed_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:  # RFC 822 (RSS)
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:  # ISO 8601 (Atom)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_feed(xml_text: str, source_id: str, feed_url: str) -> list[RawTenderNotice]:
    """Parse an RSS 2.0 or Atom feed into notices (pure — unit-testable)."""
    root = ET.fromstring(xml_text)
    notices: list[RawTenderNotice] = []

    # RSS 2.0
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        notices.append(
            RawTenderNotice(
                source_id=source_id,
                source_url=link,
                title=title,
                description=(item.findtext("description") or "").strip() or None,
                published_at=_parse_feed_datetime(item.findtext("pubDate")),
                raw={"feed_url": feed_url, "format": "rss"},
            )
        )

    # Atom
    for entry in root.iter(f"{{{_NS['atom']}}}entry"):
        title = (entry.findtext(f"{{{_NS['atom']}}}title") or "").strip()
        link_el = entry.find(f"{{{_NS['atom']}}}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        if not title or not link:
            continue
        notices.append(
            RawTenderNotice(
                source_id=source_id,
                source_url=link,
                title=title,
                published_at=_parse_feed_datetime(
                    entry.findtext(f"{{{_NS['atom']}}}updated")
                ),
                raw={"feed_url": feed_url, "format": "atom"},
            )
        )

    return notices


def parse_sitemap(xml_text: str, url_filter: str | None = None) -> list[str]:
    """Extract URLs from a sitemap.xml, optionally filtered by substring."""
    root = ET.fromstring(xml_text)
    urls = [
        loc.text.strip()
        for loc in root.iter(f"{{{_NS['sm']}}}loc")
        if loc.text
    ]
    if url_filter:
        needle = url_filter.casefold()
        urls = [u for u in urls if needle in u.casefold()]
    return urls


@register_adapter
class GenericSitemapRssAdapter(Adapter):
    key = "generic_sitemap_rss"

    def fetch(self) -> Iterator[RawTenderNotice]:
        opts = self.config.options
        headers = {"User-Agent": USER_AGENT}

        with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
            feed_url = opts.get("feed_url")
            if feed_url:
                resp = client.get(feed_url)
                resp.raise_for_status()
                yield from parse_feed(resp.text, self.config.source_id, feed_url)
                return

            sitemap_url = opts.get("sitemap_url")
            if sitemap_url:
                resp = client.get(sitemap_url)
                resp.raise_for_status()
                for url in parse_sitemap(resp.text, opts.get("url_filter", "tender")):
                    # Bare notice: the document pipeline enriches from the page.
                    yield RawTenderNotice(
                        source_id=self.config.source_id,
                        source_url=url,
                        title=url.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                        raw={"sitemap_url": sitemap_url, "format": "sitemap"},
                    )
                return

        raise ValueError(
            "generic_sitemap_rss requires options.feed_url or options.sitemap_url"
        )
