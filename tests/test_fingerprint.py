"""Discovery-mode classifier tests (Blueprint §5.2.1) — pure, no network."""

from tenderza.crawl.fingerprint import Evidence, classify


def _ev(**kw) -> Evidence:
    defaults = dict(url="https://example.gov.za/tenders", status_code=200,
                    headers={}, html="<html><body>" + "<a href='/x'>x</a>" * 20)
    defaults.update(kw)
    return Evidence(**defaults)


class TestCmsDetection:
    def test_wordpress_html_marker(self):
        fp = classify(_ev(html="<link rel='stylesheet' href='/wp-content/x.css'>"))
        assert fp.platform == "WORDPRESS"
        assert fp.adapter == "generic_cms"
        assert fp.workable

    def test_wordpress_wp_json(self):
        fp = classify(_ev(html="<link rel='https://api.w.org/' href='/wp-json/'>"))
        assert fp.platform == "WORDPRESS"

    def test_drupal_header(self):
        fp = classify(_ev(headers={"X-Generator": "Drupal 10 (https://drupal.org)"}))
        assert fp.platform == "DRUPAL"
        assert fp.adapter == "generic_cms"

    def test_drupal_files_path(self):
        fp = classify(_ev(html="<img src='/sites/default/files/logo.png'>"))
        assert fp.platform == "DRUPAL"

    def test_joomla(self):
        fp = classify(_ev(html="<script src='/media/jui/js/jquery.min.js'>"))
        assert fp.platform == "JOOMLA"


class TestFeedAndShape:
    def test_feed_beats_shape(self):
        fp = classify(_ev(feed_url="https://example.gov.za/tenders/feed"))
        assert fp.platform == "RSS"
        assert fp.adapter == "generic_sitemap_rss"

    def test_cms_beats_feed(self):
        """CMS adapter can exploit richer APIs than a bare feed."""
        fp = classify(_ev(html="x /wp-content/ y",
                          feed_url="https://example.gov.za/feed"))
        assert fp.platform == "WORDPRESS"

    def test_pdf_only(self):
        html = "<a href='/t/a.pdf'>a</a><a href='/t/b.pdf'>b</a><a href='/t/c.pdf'>c</a>"
        fp = classify(_ev(html=html, pdf_links=3, total_links=3))
        assert fp.platform == "PDF_ONLY"
        assert fp.adapter == "pdf_bulletin"
        assert fp.crawl_method == "PDF"

    def test_sitemap_fallback(self):
        fp = classify(_ev(sitemap_ok=True))
        assert fp.platform == "SITEMAP_ONLY"
        assert fp.adapter == "generic_sitemap_rss"

    def test_js_shell_needs_playwright(self):
        html = "<div id='root'></div><script src='a.js'></script><script>b()</script>"
        fp = classify(_ev(html=html, total_links=0))
        assert fp.crawl_method == "PLAYWRIGHT"
        assert not fp.workable  # bespoke queue, not silently skipped

    def test_plain_custom_html_goes_to_triage(self):
        fp = classify(_ev())
        assert fp.platform == "CUSTOM_HTML"
        assert not fp.workable


class TestBlockersNeverBypassed:
    def test_cloudflare_403(self):
        fp = classify(_ev(status_code=403, headers={"Server": "cloudflare"}))
        assert not fp.workable
        assert fp.crawl_method == "PLAYWRIGHT"
        assert any("WAF_BLOCKED" in n for n in fp.notes)
        assert any("never bypass" in n for n in fp.notes)

    def test_cf_ray_header_counts(self):
        fp = classify(_ev(status_code=503, headers={"CF-RAY": "8a1-JNB"}))
        assert any("WAF_BLOCKED" in n for n in fp.notes)

    def test_plain_404(self):
        fp = classify(_ev(status_code=404))
        assert fp.platform == "NONE"
        assert "HTTP 404" in fp.notes[0]

    def test_unreachable(self):
        fp = classify(Evidence(url="https://x", status_code=None, error="timeout"))
        assert fp.platform == "UNKNOWN"
        assert "UNREACHABLE" in fp.notes[0]


def test_backoff_schedule():
    from tenderza.crawl.scheduler import backoff_minutes
    assert [backoff_minutes(a) for a in (1, 2, 3, 4, 5, 6)] == [
        15, 30, 60, 120, 240, 240,  # capped
    ]
