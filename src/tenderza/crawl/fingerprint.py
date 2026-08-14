"""Discovery-mode platform fingerprinting (Blueprint §5.2.1).

Detects, per source: CMS platform (WordPress/Drupal/Joomla via headers and
HTML markers), RSS/Atom feeds, sitemap.xml, PDF-only tender sections,
JS-rendered shells, and WAF blocks — then recommends a generic adapter so
the long tail never needs bespoke code (§5.2.2).

Split into:
* ``classify(evidence)`` — PURE decision logic, pinned by fixture tests;
* ``probe(url)``        — polite network collection of that evidence.

Never bypasses WAFs: a 403/503 with WAF markers is *recorded*, not fought
(§5.3, §17.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from tenderza.adapters.base import USER_AGENT

# ---------------------------------------------------------------------------
# Evidence (collected) and Fingerprint (decided)
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    url: str
    status_code: int | None = None          # None = connection failed
    headers: dict[str, str] = field(default_factory=dict)
    html: str = ""
    feed_url: str | None = None             # first working RSS/Atom URL
    sitemap_ok: bool = False
    pdf_links: int = 0
    total_links: int = 0
    error: str | None = None


@dataclass
class Fingerprint:
    platform: str                            # source_platform enum value
    adapter: str | None                      # recommended adapter key, None = bespoke/manual
    crawl_method: str                        # crawl_method enum value
    confidence: float
    notes: list[str] = field(default_factory=list)

    @property
    def workable(self) -> bool:
        """True when a generic adapter can crawl this source unattended."""
        return self.adapter is not None


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------

_WAF_MARKERS = ("cloudflare", "incapsula", "sucuri", "akamai")
_PDF_LINK = re.compile(r"href=[\"'][^\"']+\.pdf", re.IGNORECASE)
_ANY_LINK = re.compile(r"<a\s", re.IGNORECASE)
_SCRIPT_TAG = re.compile(r"<script\b", re.IGNORECASE)


def _header(headers: dict[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def classify(e: Evidence) -> Fingerprint:  # noqa: PLR0911 — decision table
    """Evidence -> platform + recommended adapter. Order matters:
    hard blockers first, then CMS markers, then content-shape fallbacks."""

    if e.status_code is None:
        return Fingerprint("UNKNOWN", None, "NONE", 0.9,
                           [f"UNREACHABLE: {e.error or 'connection failed'}"])

    server = _header(e.headers, "server").lower()
    if e.status_code in (403, 429, 503) and (
        any(m in server for m in _WAF_MARKERS)
        or "cf-ray" in {k.lower() for k in e.headers}
        or any(m in e.html.lower()[:3000] for m in _WAF_MARKERS)
    ):
        return Fingerprint("UNKNOWN", None, "PLAYWRIGHT", 0.85,
                           [f"WAF_BLOCKED (HTTP {e.status_code}) — flag for "
                            "Playwright + human check; never bypass"])

    if e.status_code >= 400:
        return Fingerprint("NONE", None, "NONE", 0.7,
                           [f"HTTP {e.status_code} on tender URL"])

    html_l = e.html.lower()

    # --- CMS markers (take precedence: generic_cms exploits their APIs) ---
    if ("wp-content" in html_l or "wp-json" in html_l
            or "wordpress" in _header(e.headers, "x-powered-by").lower()):
        return Fingerprint("WORDPRESS", "generic_cms", "HTML", 0.9,
                           ["WordPress markers found (wp-content/wp-json)"])

    if (_header(e.headers, "x-generator").lower().startswith("drupal")
            or "drupal.settings" in html_l or "/sites/default/files" in html_l):
        return Fingerprint("DRUPAL", "generic_cms", "HTML", 0.9,
                           ["Drupal markers found"])

    if "joomla" in html_l[:5000] or "/media/jui/" in html_l:
        return Fingerprint("JOOMLA", "generic_cms", "HTML", 0.85,
                           ["Joomla markers found"])

    # --- Feed / sitemap (zero-per-site-code adapters, §5.2.2) ---
    if e.feed_url:
        return Fingerprint("RSS", "generic_sitemap_rss", "HTML", 0.85,
                           [f"working feed: {e.feed_url}"])

    # --- Content shape ---
    if e.total_links > 0 and e.pdf_links / e.total_links >= 0.5 and e.pdf_links >= 3:
        return Fingerprint("PDF_ONLY", "pdf_bulletin", "PDF", 0.75,
                           [f"{e.pdf_links}/{e.total_links} links are PDFs"])

    if e.sitemap_ok:
        return Fingerprint("SITEMAP_ONLY", "generic_sitemap_rss", "HTML", 0.7,
                           ["sitemap.xml present; no feed/CMS markers"])

    # JS shell: tiny body, few links, script-heavy
    if len(e.html) < 3000 and e.total_links < 5 and len(_SCRIPT_TAG.findall(e.html)) >= 2:
        return Fingerprint("CUSTOM_HTML", None, "PLAYWRIGHT", 0.6,
                           ["JS-rendered shell — needs Playwright adapter"])

    return Fingerprint("CUSTOM_HTML", None, "HTML", 0.5,
                       ["no generic markers — triage for bespoke spider (§5.2.3)"])


# ---------------------------------------------------------------------------
# Network probe (polite)
# ---------------------------------------------------------------------------

_FEED_PATHS = ("feed", "rss", "feed/", "?feed=rss2", "rss.xml", "atom.xml")


def probe(url: str, *, timeout: float = 20.0) -> Evidence:
    """Collect classification evidence for one source URL. One page GET,
    a few cheap probes; failures recorded, never raised."""
    ev = Evidence(url=url)
    headers = {"User-Agent": USER_AGENT}

    try:
        with httpx.Client(headers=headers, timeout=timeout,
                          follow_redirects=True) as client:
            resp = client.get(url)
            ev.status_code = resp.status_code
            ev.headers = dict(resp.headers)
            ev.html = resp.text[:500_000]
            ev.pdf_links = len(_PDF_LINK.findall(ev.html))
            ev.total_links = len(_ANY_LINK.findall(ev.html))

            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"

            # Feed probe: page URL first (WordPress /feed), then site root.
            for candidate_base in (url.rstrip("/") + "/", base):
                for path in _FEED_PATHS:
                    try:
                        r = client.get(urljoin(candidate_base, path))
                        ctype = r.headers.get("content-type", "")
                        body = r.text[:200].lstrip().lower()
                        if r.status_code == 200 and (
                            "xml" in ctype
                            or body.startswith("<?xml")
                            or "<rss" in body
                            or "<feed" in body
                        ):
                            ev.feed_url = str(r.url)
                            break
                    except httpx.HTTPError:
                        continue
                if ev.feed_url:
                    break

            try:
                r = client.head(urljoin(base, "sitemap.xml"))
                ev.sitemap_ok = r.status_code == 200
            except httpx.HTTPError:
                pass

    except httpx.HTTPError as exc:
        ev.error = f"{type(exc).__name__}: {exc}"

    return ev
