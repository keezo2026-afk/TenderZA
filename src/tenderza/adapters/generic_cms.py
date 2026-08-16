"""Generic CMS adapter — WordPress / Drupal / Joomla (Blueprint §5.2.2).

Strategy, in order of preference:
1. WordPress REST API (``/wp-json/wp/v2/posts?search=tender``) when detected;
2. RSS/Atom feed (delegates to the sitemap/RSS parser);
3. Fallback: structured listing extraction (Phase 1+ — requires Scrapy;
   this skeleton raises NotImplementedError for that path so the scheduler
   flags the source for the bespoke/Playwright queue instead of failing
   silently).

Platform fingerprinting (§5.2.1) fills SourceConfig.options["platform"]
during discovery mode; this adapter trusts it and probes gently otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timezone
from typing import Any

import httpx

from tenderza.adapters.base import (
    USER_AGENT,
    Adapter,
    RawTenderNotice,
    register_adapter,
)
from tenderza.adapters.generic_sitemap_rss import parse_feed
from tenderza.timeutil import ensure_tz, parse_iso


def wp_post_to_notice(post: dict[str, Any], source_id: str) -> RawTenderNotice:
    """Map a WordPress REST API post object to a notice (pure)."""
    title = (post.get("title") or {}).get("rendered", "").strip()
    # WordPress: date_gmt is genuinely UTC (the field contract says so);
    # the bare `date` field is site-local, which for SA sources is SAST.
    published = post.get("date_gmt")
    published_at = ensure_tz(parse_iso(published), assume=timezone.utc)
    if published_at is None:
        published_at = ensure_tz(parse_iso(post.get("date")))
    return RawTenderNotice(
        source_id=source_id,
        source_url=post.get("link", ""),
        title=title or f"wp-post-{post.get('id')}",
        description=(post.get("excerpt") or {}).get("rendered") or None,
        published_at=published_at,
        raw=post,
    )


@register_adapter
class GenericCmsAdapter(Adapter):
    key = "generic_cms"

    def fetch(self) -> Iterator[RawTenderNotice]:
        opts = self.config.options
        platform = (opts.get("platform") or "").casefold()
        base = self.config.crawl_url.rstrip("/")
        headers = {"User-Agent": USER_AGENT}

        with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
            # 1. WordPress JSON API
            if platform in ("", "wordpress"):
                wp_url = opts.get("wp_api_url") or f"{base}/wp-json/wp/v2/posts"
                try:
                    resp = client.get(
                        wp_url,
                        params={"search": opts.get("search", "tender"), "per_page": 50},
                    )
                    if resp.status_code == 200 and resp.headers.get(
                        "content-type", ""
                    ).startswith("application/json"):
                        for post in resp.json():
                            yield wp_post_to_notice(post, self.config.source_id)
                        return
                except httpx.HTTPError:
                    if platform == "wordpress":
                        raise  # declared WordPress but API failed — surface it

            # 2. RSS/Atom feed fallback
            feed_url = opts.get("feed_url") or f"{base}/feed"
            try:
                resp = client.get(feed_url)
                if resp.status_code == 200 and "<" in resp.text[:200]:
                    yield from parse_feed(resp.text, self.config.source_id, feed_url)
                    return
            except httpx.HTTPError:
                pass

        # 3. Structured listing extraction — bespoke/Scrapy territory.
        raise NotImplementedError(
            f"{self.config.name}: no WP API or feed detected; "
            "route to bespoke-spider / Playwright triage queue (§5.2.3)"
        )
