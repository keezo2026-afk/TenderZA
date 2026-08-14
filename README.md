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
```

## Doctrine (non-negotiable, §6/§17)

- The crawler is **deterministic**; AI runs only downstream of download, never as a crawl decision.
- Respect `robots.txt`, rate limits, and WAFs. **Never** bypass CAPTCHAs or WAFs.
- Summaries + links only — never republish scraped tender documents. eTender OCDS
  data is used under **CC BY 4.0** with attribution.
- An unverified closing date never drives user alerts (§10.3).
- Every adapter ships with golden fixtures (`tests/fixtures/adapters/`) — the
  knowledge-loss and adapter-rot mitigation of §19.

## Phase 0 open items

- [ ] Verify the OCDS API base URL, pagination and rate limits from the OpenAPI spec
      at [data.etenders.gov.za](https://data.etenders.gov.za/) (§3.1). The default in
      `ocds_api.py` is community-documented and must be confirmed — the endpoint was
      not reachable from the build sandbox.
- [ ] Verify/repair the seeded `tender_url`s during discovery mode (§5.2) — several
      provincial/metro URLs are best-effort placeholders flagged `DISCOVERY`.
- [ ] Business model sign-off (§2.3) and PPA-watch task setup (§17.3).
