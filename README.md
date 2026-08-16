# TenderZA

South African Tender Discovery & Intelligence Platform — a "Google for South African
tenders" that continuously discovers, extracts, normalizes, deduplicates and analyzes
procurement notices across every reachable public-sector source.

**Design authority:** [`docs/blueprint-v2.md`](docs/blueprint-v2.md) (Blueprint v2.0,
approved for Phase 0/1 planning). All section references (§) below point there.

## Status: Phase 0/1 skeleton

| Component | Blueprint | Where |
|---|---|---|
| Database schema v2 (pgvector, provenance, review queue, append-only crawl log) | §10–11 | [`db/schema.sql`](db/schema.sql) |
| Source Registry seed (eTender + 9 provinces + 8 metros + GTB) | §4 | [`src/tenderza/registry/`](src/tenderza/registry/) |
| Generic adapter framework + registry | §5 | [`src/tenderza/adapters/base.py`](src/tenderza/adapters/base.py) |
| eTender OCDS API ingestor (+ OpenAPI verification helper) | §3.1, §5.1 | [`src/tenderza/adapters/ocds_api.py`](src/tenderza/adapters/ocds_api.py) |
| Generic sitemap/RSS adapter | §5.2 | [`src/tenderza/adapters/generic_sitemap_rss.py`](src/tenderza/adapters/generic_sitemap_rss.py) |
| Generic CMS adapter (WordPress-first) | §5.2 | [`src/tenderza/adapters/generic_cms.py`](src/tenderza/adapters/generic_cms.py) |
| PDF-bulletin adapter (content-hash diffing) | §5.2 | [`src/tenderza/adapters/pdf_bulletin.py`](src/tenderza/adapters/pdf_bulletin.py) |
| Tender-number normalization (dedupe backbone) + golden corpus | §7 | [`src/tenderza/normalize/`](src/tenderza/normalize/), [`tests/fixtures/tender_numbers.json`](tests/fixtures/tender_numbers.json) |
| Pipeline: normalizer w/ provenance, entity resolution, dedupe + authority merge, computed status | §5.4, §7, §8, §10 | [`src/tenderza/pipeline/`](src/tenderza/pipeline/) |
| Persistence: tender upsert + version diffs, OCDS mirror archive, review-queue writes | §9, §10.2.6 | [`src/tenderza/persistence/`](src/tenderza/persistence/) |
| Runnable OCDS ingest (poll / windowed historical backfill / file mode) | §3.1 | [`scripts/ingest_ocds.py`](scripts/ingest_ocds.py) |
| FastAPI read layer: FTS search, filters, tender detail w/ versions, stats | §12 | [`src/tenderza/api/`](src/tenderza/api/) |
| One-command demo (embedded Postgres + live/sample ingest + API) | — | [`scripts/dev_demo.py`](scripts/dev_demo.py) |
| Next.js search UI (FTS search, filters, tender detail w/ provenance + history) | §12, P1 UI-v1 | [`web/`](web/) |
| Crawl scheduler: state machine, backoff, source health, append-only results | §5.3 | [`src/tenderza/crawl/scheduler.py`](src/tenderza/crawl/scheduler.py), [`scripts/run_crawler.py`](scripts/run_crawler.py) |
| Discovery-mode platform fingerprinting (CMS/RSS/sitemap/PDF/WAF detection) | §5.2 | [`src/tenderza/crawl/fingerprint.py`](src/tenderza/crawl/fingerprint.py), [`scripts/discover_sources.py`](scripts/discover_sources.py) |
| Document pipeline: hash-keyed object store, PDF text extraction w/ OCR detection, rules-based field extraction w/ confidence, tender enrichment + review queue | §6 | [`src/tenderza/documents/`](src/tenderza/documents/), [`scripts/process_documents.py`](scripts/process_documents.py) |
| Review-queue admin: API (approve/correct/reject w/ human provenance + versioning) and UI at `/review` | §6 human-in-the-loop | [`src/tenderza/api/review.py`](src/tenderza/api/review.py), [`web/app/review/`](web/app/review/) |
| Email alerts v1: saved searches, batched digests, at-most-once, verified-dates-only deadlines; UI at `/alerts` | §14 (P2 MLP) | [`src/tenderza/alerts/`](src/tenderza/alerts/), [`scripts/run_alerts.py`](scripts/run_alerts.py) |
| Source-health dashboard + alerting: SLA/MTTD/MTTR classification, `/ops/*` API, `/ops` UI, cron pager | §15, §16 (P2 MLP) | [`src/tenderza/health/`](src/tenderza/health/), [`web/app/ops/`](web/app/ops/), [`scripts/check_source_health.py`](scripts/check_source_health.py) |
| Local dev stack (Postgres+pgvector, Redis, MinIO) | §18 | [`infra/docker-compose.yml`](infra/docker-compose.yml) |
| CI (lint + tests + schema-apply + registry seed) | §20 | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |

## Quick start

```bash
# 1. Install (Python 3.10+)
pip install -e ".[dev]"

# 2. Run the test suite
pytest

# 3. Bring up the dev stack and apply the schema
docker compose -f infra/docker-compose.yml up -d
psql postgresql://tenderza:tenderza@localhost:5432/tenderza -f db/schema.sql

# 4. Seed the source registry
DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
    python scripts/seed_registry.py

# 5. Pull live tenders from the (verified) eTender OCDS API
DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
    python scripts/ingest_ocds.py --days 7

# 5b. Historical backfill (~158k releases since May 2021, month-windowed)
DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
    python scripts/ingest_ocds.py --backfill 2021-05

# 6. Run store integration tests against your local Postgres (optional)
TEST_DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza pytest

# 7. Serve the read API
DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
    uvicorn tenderza.api.app:app --host 0.0.0.0 --port 8000
```

### Zero-setup demo (no Docker needed)

```bash
pip install -e ".[dev]"
python scripts/dev_demo.py            # embedded Postgres + schema + seed
                                      # + live OCDS pull (or bundled real
                                      # sample data offline) + API on :8000
```

Then: `GET /tenders?q=cctv`, `GET /tenders?province=Western+Cape&status=OPEN`,
`GET /tenders?compulsory_briefing=true&closing_within_days=30`,
`GET /tenders/{id}` (documents + version history + per-field provenance),
`GET /stats`, `GET /docs` (OpenAPI UI).

### Web UI

```bash
cd web && npm install && npm run build
API_URL=http://127.0.0.1:8000 npx next start -H 0.0.0.0 -p 3000
```

Search page with FTS + province/status/deadline/compulsory-briefing filters;
tender detail with key dates, per-field provenance dots, documents (linked to
the official source, never re-hosted), version history, and full source
attribution. The browser only talks to Next.js; `/api/*` is proxied
server-side to FastAPI (`API_URL`, default `http://127.0.0.1:8000`).

### Crawling the registry

```bash
# Fingerprint DISCOVERY sources & assign generic adapters (§5.2)
DATABASE_URL=... python scripts/discover_sources.py
python scripts/discover_sources.py --url https://some.gov.za/tenders  # one-off, no DB

# Run the scheduler: enqueue due sources, execute jobs, retry with backoff (§5.3)
DATABASE_URL=... python scripts/run_crawler.py            # one pass
DATABASE_URL=... python scripts/run_crawler.py --loop 300 # continuous
```

Discovery probes each source politely (one page + feed/sitemap checks),
classifies the platform (WordPress/Drupal/Joomla → `generic_cms`, feeds →
`generic_sitemap_rss`, PDF-heavy → `pdf_bulletin`, WAF/JS → triage queue —
never bypassed), and promotes workable sources to `ACTIVE`. The scheduler
walks `PENDING → FETCHING → PARSING → DONE | RETRY_BACKOFF | FAILED` with
exponential backoff (15→240 min, 5 attempts), writes append-only
`crawl_results`, logs every run to `source_health`, and pushes notices
through the same normalize→dedupe→version pipeline as the OCDS ingest.

### Document pipeline (§6)

```bash
DATABASE_URL=... python scripts/process_documents.py --store-root ./data/objects
```

Fetches unprocessed `tender_documents`, stores bytes by SHA-256 content hash
(same bulletin on two pages = one object), extracts text via PyMuPDF
(textless/scanned PDFs are flagged `needs_ocr` for the OCR stage — never
faked), runs the rules-based field extractor (tender number, closing
date/time, briefing + compulsory flag, CIDB grades, B-BBEE level, contacts,
estimated value — each with confidence + evidence snippet), then enriches
tenders under the confidence rule: **a document never overrides a
higher-confidence field** (the portal's closing date always beats a PDF's),
and high-stakes fields below 0.80 land in `review_queue` with evidence for
human confirmation.

### Review queue (human-in-the-loop, §6)

The `/review` page in the web UI lists open items (deadlines first) with the
extracted value, its confidence, the matched evidence snippet, and a
"verify at source" link. Reviewer actions:

- **Approve** — value confirmed; applied with provenance
  `{SOURCE, human:<name>, confidence 1.0}`.
- **Correct** — reviewer supplies the right value; applied the same way.
- **Reject** — extraction was wrong; item closes, tender untouched.

Applied changes write a `HUMAN_VERIFIED` version row (§9) and — for closing
dates — recompute the tender's real status (`OPEN`/`CLOSING_SOON`/`CLOSED`),
which is what unlocks deadline alerts (§10.3: unverified dates never alert).
The API endpoints are unauthenticated in v1 (single-operator P1 admin) —
role-gate before any public deployment.

### Email alerts v1 (§14, P2 MLP)

```bash
# Cron entry point — SMTP if configured, dev outbox (.eml files) otherwise
DATABASE_URL=... [SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=...] \
    python scripts/run_alerts.py
```

Users save searches at `/alerts` (email + keywords + province + optional
closing window). Each engine run sends **one batched digest per alert**
(no per-tender spam), records every (alert, tender) pair in `alert_events`
so nothing is ever notified twice, and enforces the §14 hard rules: the
engine reads only the normalized tender table; closing-window filters use
**verified dates only**; unverified dates appear in digests as
"UNVERIFIED — verify at source" with the date withheld; emails carry
summaries + links only (never documents). Alert-precision KPI (§16) reads
from `alert_events.sent_at` vs `clicked_at`.

### Source-health dashboard & alerting (§15, §16, P2 MLP)

```bash
# Cron/systemd pager — exit 0 = all clear, 1 = action required, 2 = cannot run
DATABASE_URL=... python scripts/check_source_health.py --notify ops@example.com
```

Every registered source is classified into one of seven states —
`OK · STALE · DEGRADED · FAILED · NEVER_RUN · DISCOVERY · PUBLISH_NOTHING` —
by pure, clock-injected logic in [`src/tenderza/health/metrics.py`](src/tenderza/health/metrics.py),
so the same verdict drives the dashboard, the API and the pager and they can
never disagree. Staleness is `max(tier SLA, 2 × polling interval)`, so a
daily-tier source is not called stale ten minutes after its window opens, and
a source known to publish nothing never pages anyone.

| Tier | Freshness SLA (§16) | MTTD target |
|------|--------------------|-------------|
| T1 (national portals) | 30 min | 4 h |
| T2 (metros, big provinces) | 2 h | 4 h |
| T3 (secondary munis) | 6 h | 24 h |
| T4 (long tail) | 24 h | 24 h |

The `/ops` page (nav: **Health**) shows the alarm banner first — the same list
the pager emails — then the §16 KPIs (≥95% of crawlable sources inside SLA,
≥99% job success), per-tier freshness, adapter health, the per-source table and
30-day MTTR. Adapter rot is caught by the **silent** flag: runs that succeed but
yield zero tenders, the signature of a site redesign that quietly broke a parser.

`source_health.mttd_started_at` is stamped by the scheduler: a failure opens an
episode (or carries the open one forward), a success closes it with `NULL`, so
MTTD/MTTR is a subtraction rather than a reconstruction over the whole log.
API: `/ops/overview`, `/ops/sources`, `/ops/alarms`, `/ops/crawl`,
`/ops/freshness`, `/ops/adapters`, `/ops/failures`. `/ops/alarms` returns 200
with an empty list when all is well and is safe for an external monitor to poll.

## Doctrine (non-negotiable, §6/§17)

- The crawler is **deterministic**; AI runs only downstream of download, never as a crawl decision.
- Respect `robots.txt`, rate limits, and WAFs. **Never** bypass CAPTCHAs or WAFs.
- Summaries + links only — never republish scraped tender documents. eTender OCDS
  data is used under **CC BY 4.0** with attribution.
- An unverified closing date never drives user alerts (§10.3).
- Every adapter ships with golden fixtures (`tests/fixtures/adapters/`) — the
  knowledge-loss and adapter-rot mitigation of §19.

## Phase 0 open items

- [x] ~~Verify the OCDS API base URL, pagination and rate limits~~ — **done, live,
      14 Aug 2026**: `https://ocds-api.etenders.gov.za/api/OCDSReleases`
      (PageNumber/PageSize/dateFrom/dateTo, `links.next` pagination, PageSize up to
      1000 browser / ~20k API clients, OCDS 1.1, ocid prefix `ocds-9t57fa`).
      A real release is pinned as a golden fixture in
      `tests/fixtures/adapters/ocds_api/real_release_2026-08-14.json`.
- [ ] Verify/repair the seeded `tender_url`s during discovery mode (§5.2) — several
      provincial/metro URLs are best-effort placeholders flagged `DISCOVERY`.
- [ ] Business model sign-off (§2.3) and PPA-watch task setup (§17.3).
