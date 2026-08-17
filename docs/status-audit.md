# TenderZA — Blueprint vs. Reality

| | |
|---|---|
| **Date** | 2026-08-17 |
| **Commit audited** | `74d8972` (branch `arena/01a00af8-tenderza`) |
| **Method** | Every number below was measured by running the code, querying a live database, or probing the network — not read off the README |
| **Authority** | [`blueprint-v2.md`](blueprint-v2.md) §20 roadmap and per-phase acceptance criteria |

## 1. Executive summary

**The machinery is built. The coverage is not.**

Every subsystem the blueprint asks for through Phase 2 exists, is wired
end-to-end, and is tested: ingestion, normalization, dedupe, versioning,
document extraction, review queue, search, alerts, source-health, auth. That
is genuinely most of the hard engineering, and it works — the demo boots from
scratch and serves real eTender data.

But the product's value proposition is **coverage**, and coverage is at
Phase 0. One source is live. The blueprint's MLP needs ~65.

The single most important thing to understand: **the remaining work is not
mostly code.** It is per-site onboarding — fingerprinting ~65 government
websites, seeing which yield to the generic adapters, and writing bespoke
extraction for those that don't. The generic-adapter framework was built
precisely to make that affordable, and it has never been run against a real
municipal site.

The second most important thing: **CI has never executed.** Not once. That is
why a defect that broke the entire test suite reached `main` unnoticed.

## 2. Measured baseline

Reproduce with `python scripts/dev_demo.py` (bundled sample) and `pytest`:

| Metric | Measured | MLP target (§20 P2) | Gap |
|---|---:|---:|---|
| Sources `ACTIVE` | **1** | ~65 | **64** |
| Sources `DISCOVERY` (placeholder) | **19** | 0 | 19 to triage |
| Provinces crawling | **0** | 9 | 9 |
| Municipalities crawling | **0** | 50 | 50 |
| SOEs *in the registry at all* | **0** | 5 | 5 |
| Tenders in DB | **5** | ~5,000 | ~4,995 |
| Sources monitored by dashboard | 20 registered / **1** reporting | >80% | — |
| Backend tests passing | **346** | — | — |
| Backend tests **skipped** | **197 (36%)** | — | needs Postgres |
| Frontend tests | 17 pass | — | — |
| **CI runs ever executed** | **0** | — | **blocking** |

Source registry composition (`seed_sources.json`, 20 entries):

| | Count | Note |
|---|---:|---|
| `NATIONAL` | 3 | only the OCDS API is `ACTIVE` |
| `PROVINCE` | 9 | all `DISCOVERY` |
| `METRO` | 8 | all `DISCOVERY` |
| `SOE` | **0** | P2 requires 5 — **none are registered** |

**18 of 20 sources are assigned `generic_cms`**, whose fallback path raises
`NotImplementedError` by design when a site exposes no WordPress API and no
RSS feed. Whether those 18 sites yield to it is **unknown and untested** —
that is the central unvalidated assumption in the plan.

## 3. Per-phase assessment

| Phase | Blueprint status | Actual | Evidence |
|---|---|---|---|
| **P0** Design & registry | Complete | **Complete** | Blueprint v2, schema, 20 sources seeded, OCDS endpoint verified live |
| **P1** Prototype | Complete | **Substantially complete** | eTender ingest, dedupe, extraction w/ confidence, review queue, search UI all work. **Misses "5 municipalities + 1 province"** — 0 non-OCDS sources ingest |
| **P2** Expansion → MLP | Complete | **Machinery yes, coverage no** | Alerts v1, source-health, generic adapters all built + tested. 1/65 sources. 5/5000 tenders. **MLP not achieved** |
| **P3** 257 municipalities | Not started | Not started | Discovery tooling exists, never run against a live site |
| **P4** Fit score v1 | Not started | Not started | No `fit_score`/`match_score` anywhere in `src/` |
| **P5** AI extraction v2 | Not started | **Partially blocked** | Multilingual synonyms live. No gold corpus beyond tender numbers → the ">90% field accuracy" criterion is **currently unmeasurable** |
| **P6** Alerts v2 | Not started | Not started | Email only; schema has `channels` jsonb ready |
| **P7** Analytics | Not started | Not started | — |
| **P8** Scale & PPA | Not started | Not started | PPA-watch is a standing task, no tracker in repo |

## 4. Findings

### F1 — CI has never run *(blocking, cheap to fix)*

There is no `.github/workflows/`. The workflow sits unused at
`ci/github-ci.yml` because the GitHub app connection lacks the `workflows`
permission, as documented in `ci/README.md`.

Consequences, both observed rather than hypothesised:

- A `from tests.conftest import DSN` in `test_search_integration.py` broke
  **collection of the entire suite** — every test, not just that file — and
  reached `main`. Fixed in `5a5e1c8`.
- `npm test` quoted its glob so that cmd.exe matched zero files **and still
  exited 0**: a green run that tested nothing. Fixed in `74d8972`.

Both are exactly what CI exists to catch. Until a human moves that file,
every check is manual and the repo will keep accumulating this class of bug.

### F2 — The generic-adapter bet is unvalidated

The plan's affordability rests on generic adapters absorbing most of 257
municipalities (§5.2). 18 sources are assigned `generic_cms`; **none has ever
been fetched**. If the typical SA municipal site is ASP.NET with `__VIEWSTATE`
and no feed — plausible, given the `.aspx` URLs already in the registry — the
generic path degrades to the bespoke queue and P3's 10–14 PM estimate is
optimistic.

**This is the highest-information, lowest-cost experiment available.** Probing
19 URLs takes minutes and either de-risks the roadmap or corrects it early.
It cannot be done from this sandbox (see F5).

### F3 — Zero SOEs registered

P2 names "5 SOEs" in its acceptance criteria. The registry contains none —
not even as `DISCOVERY` placeholders. Eskom and Transnet already appear as
*buyers* in ingested OCDS data, so they arrive incidentally via eTender, but
no SOE portal is a registered source. This is a gap in the plan's own terms
that nothing currently tracks.

### F4 — Categories are populated but unreachable

eTender supplies real category values — measured in the live DB:
`Services: Professional`, `Supplies: Electrical Equipment`,
`Supplies: General`, `Services: General`, `Sewerage`. They are normalized,
stored, and the alert matcher filters on them (`t.categories ?| %s`).

But there is **no category filter in the search UI and no facet endpoint in
the API**. A user cannot discover which categories exist, so the alert feature
that depends on them is effectively undiscoverable. Small, self-contained, and
fully testable offline — good candidate work.

### F5 — Source work cannot be done in the Arena sandbox

Verified this session: all 19 `gov.za` URLs fail with `ConnectError`, and so
does the eTender OCDS API that succeeded earlier in the same session, while
`pypi.org` returns 200. Egress is filtered to package registries.

**Implication:** adapter and onboarding work must run on a network-capable
machine. An agent here can build and unit-test the tooling, but cannot verify
it against a live site — and shipping unverifiable crawler code is how silent
adapter rot starts. Split the work accordingly.

### F6 — Documentation overstates delivery

- README's status table links CI to `.github/workflows/ci.yml`. **That file
  does not exist.**
- The table reads as "everything shipped" because it maps components to
  files. It never states that 19/20 sources are inert placeholders, which is
  the single fact most likely to mislead a reader about project status.

Corrected in this commit.

### F7 — Two acceptance criteria are currently unmeasurable

- **P5, ">90% field accuracy on gold corpus":** the only gold corpus is
  `tests/fixtures/tender_numbers.json`. No annotated document set exists, so
  accuracy cannot be computed — the criterion cannot be passed or failed.
- **P2, "dashboard monitoring >80% of sources":** health metrics derive from
  crawl runs. With one crawlable source, the figure is 1/20 or 1/1 depending
  on denominator, and the blueprint does not say which. Worth pinning down
  before it is reported to anyone.

Neither blocks work today; both should be settled before the numbers are
quoted externally.

### F8 — Known-inert subsystems (correctly built, intentionally dormant)

Not defects — recording them so nobody assumes they are live:

- **Semantic/pgvector search** — plumbed through, returns `None` from the
  query embedder until a model is configured; search degrades to keyword-only
  rather than failing. `tender_embeddings` is empty.
- **OCR** — scanned PDFs are flagged `needs_ocr`; no Tesseract/OCRmyPDF stage
  is implemented. Deliberately never faked.
- **`generic_cms` listing extraction** — raises `NotImplementedError` to route
  a source to the bespoke queue instead of failing silently.

## 5. Recommended sequence

1. **Enable CI** *(hours)* — move `ci/github-ci.yml` to
   `.github/workflows/ci.yml`. Needs a human with `workflows` permission.
   Everything else is riskier while this is off.
2. **Run discovery against the 19 placeholders** *(days, needs network)* —
   settles F2, the roadmap's biggest unknown, before more code is written
   against an unvalidated assumption.
3. **Promote whatever yields**, and honestly count the rest as bespoke work.
   Register the 5 SOEs (F3) while in the registry.
4. **Close small product gaps** — category facet + UI filter (F4). Offline-safe.
5. **Build the gold corpus** (F7) once real documents from varied sources
   exist; annotating only eTender PDFs would overfit the extractor.

## 6. Bottom line

The blueprint's engineering is in good shape and the code quality is high —
provenance, timezone correctness, confidence thresholds and human-in-the-loop
review are handled with real care, and the P0 SAST-as-`Z` fix shows the
data-quality discipline is genuine.

What the project needs next is **not more subsystems**. It is switching on CI,
then finding out whether the generic adapters actually work against South
African government websites. Everything downstream of that answer — P3's
effort estimate, the MLP date, the coverage KPI — is currently a guess.
