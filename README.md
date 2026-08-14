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
