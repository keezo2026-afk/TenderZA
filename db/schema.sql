-- ============================================================================
-- TenderZA — Database schema v2
-- Derived from Blueprint v2.0 §10 (data model) and §11 (tables).
-- Target: PostgreSQL 15+ with pgvector.
-- Conventions:
--   * All instants are TIMESTAMPTZ (blueprint §10.2.1 — SAST-correct closing logic).
--   * Flexible / regulation-sensitive structures are jsonb (§10.2.4).
--   * crawl_results is append-only (§5.3) — enforced by trigger.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector (tender_embeddings)

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------

CREATE TYPE org_type AS ENUM (
    'METRO', 'DISTRICT', 'LOCAL', 'PROVINCE', 'NATIONAL',
    'SOE', 'PUBLIC_ENTITY', 'UNIVERSITY_TVET', 'AGGREGATOR'
);

-- §4: platform is detected, not guessed
CREATE TYPE source_platform AS ENUM (
    'CUSTOM_HTML', 'WORDPRESS', 'DRUPAL', 'JOOMLA', 'SITEMAP_ONLY',
    'RSS', 'DOC_MGMT', 'OCDS_API', 'PDF_ONLY', 'IMAGE_ONLY', 'NONE', 'UNKNOWN'
);

CREATE TYPE crawl_method AS ENUM ('API', 'HTML', 'PLAYWRIGHT', 'PDF', 'NONE');

CREATE TYPE source_status AS ENUM (
    'ACTIVE', 'DEGRADED', 'FAILED', 'PUBLISH_NOTHING', 'INACTIVE', 'DISCOVERY'
);

-- §10.3 status values
CREATE TYPE tender_status AS ENUM (
    'NEW', 'OPEN', 'CLOSING_SOON', 'CLOSED', 'EXTENDED',
    'CANCELLED', 'AWARDED', 'RE_ADVERTISED', 'UNKNOWN'
);

-- §5.3 crawl state machine
CREATE TYPE crawl_state AS ENUM (
    'PENDING', 'FETCHING', 'PARSING', 'DONE', 'FAILED', 'RETRY_BACKOFF'
);

-- §6 / §10.2.5 field provenance
CREATE TYPE provenance_kind AS ENUM ('SOURCE', 'DERIVED', 'INFERRED');

-- ---------------------------------------------------------------------------
-- Organisations & entity resolution (§7)
-- ---------------------------------------------------------------------------

CREATE TABLE organisations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    type           org_type NOT NULL,
    province       text,
    municipality   text,                     -- municipality code/name if applicable
    parent_org_id  uuid REFERENCES organisations(id),  -- province -> district -> local roll-up
    website        text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_organisations_type ON organisations(type);
CREATE INDEX idx_organisations_province ON organisations(province);

-- "eThekwini Municipality" / "City of eThekwini" collapse to one org (§7)
CREATE TABLE organisation_aliases (
    id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id  uuid NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    alias   text NOT NULL,                   -- normalized (casefolded, squashed)
    active  boolean NOT NULL DEFAULT true,
    UNIQUE (alias)
);

CREATE INDEX idx_org_aliases_org ON organisation_aliases(org_id);

-- ---------------------------------------------------------------------------
-- Source registry (§4)
-- ---------------------------------------------------------------------------

CREATE TABLE sources (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid REFERENCES organisations(id),
    name            text NOT NULL,
    crawl_url       text,
    platform        source_platform NOT NULL DEFAULT 'UNKNOWN',
    crawl_method    crawl_method NOT NULL DEFAULT 'NONE',
    adapter         text,                    -- adapter key, e.g. 'ocds_api', 'generic_cms'
    adapter_config  jsonb NOT NULL DEFAULT '{}'::jsonb,
    triage_tier     smallint,                -- 1..4; NULL while in DISCOVERY
    authority_score smallint NOT NULL DEFAULT 50
                    CHECK (authority_score BETWEEN 0 AND 100),   -- §8
    frequency_min   integer,                 -- crawl frequency in minutes (per tier)
    last_checked    timestamptz,
    last_success    timestamptz,
    status          source_status NOT NULL DEFAULT 'DISCOVERY',
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sources_status ON sources(status);
CREATE INDEX idx_sources_tier ON sources(triage_tier);

-- ---------------------------------------------------------------------------
-- Tenders (§10, §11)
-- ---------------------------------------------------------------------------

CREATE TABLE tenders (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_number             text,
    normalized_tender_number  text,          -- output of the spec'd pure function (§7)
    fingerprint               text,          -- hash(buyer + norm number + title + closing) (§7)
    title                     text NOT NULL,
    description               text,
    buyer_id                  uuid REFERENCES organisations(id),
    province                  text,
    district                  text,
    municipality              text,
    status                    tender_status NOT NULL DEFAULT 'UNKNOWN',
    published_at              timestamptz,
    closing_at                timestamptz,   -- TIMESTAMPTZ, never bare dates (§10.2.1)
    briefing_at               timestamptz,
    compulsory_briefing       boolean,       -- NULL = unknown (§10.2.2)
    value_estimated           numeric(18,2), -- nullable: value is sparse (§10.2.3)
    currency                  char(3) DEFAULT 'ZAR',
    requirements              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- PPA-proof blob (§10.2.4)
    categories                jsonb NOT NULL DEFAULT '[]'::jsonb,
    contact                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    original_url              text,
    source_urls               jsonb NOT NULL DEFAULT '[]'::jsonb,
    field_provenance          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- per-field source/confidence (§10.2.5)
    ocds_ocid                 text,          -- link back to the OCDS mirror layer
    search_vector             tsvector GENERATED ALWAYS AS (
                                  setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                                  setweight(to_tsvector('english', coalesce(description, '')), 'B')
                              ) STORED,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CHECK (closing_at IS NULL OR published_at IS NULL OR closing_at >= published_at)  -- §6 validation
);

CREATE UNIQUE INDEX idx_tenders_fingerprint ON tenders(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX idx_tenders_norm_number ON tenders(normalized_tender_number);
CREATE INDEX idx_tenders_buyer ON tenders(buyer_id);
CREATE INDEX idx_tenders_status ON tenders(status);
CREATE INDEX idx_tenders_closing ON tenders(closing_at);
CREATE INDEX idx_tenders_province ON tenders(province);
CREATE INDEX idx_tenders_search ON tenders USING gin(search_vector);
CREATE INDEX idx_tenders_ocid ON tenders(ocds_ocid);

CREATE TABLE tender_documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id    uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    doc_url      text NOT NULL,
    filename     text,
    content_hash text,                        -- object storage keyed by content hash (§5.1)
    object_key   text,
    version      text NOT NULL DEFAULT 'v1',
    fetched_at   timestamptz
);

CREATE INDEX idx_tender_documents_tender ON tender_documents(tender_id);
CREATE INDEX idx_tender_documents_hash ON tender_documents(content_hash);

-- §9 change detection & versioning — full history retained
CREATE TABLE tender_versions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id   uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    version_no  integer NOT NULL,
    changes     jsonb NOT NULL,              -- field diffs
    change_kind text,                        -- EXTENDED / ADDENDUM / CANCELLED / ...
    detected_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tender_id, version_no)
);

-- ---------------------------------------------------------------------------
-- Awards & suppliers
-- ---------------------------------------------------------------------------

CREATE TABLE suppliers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    registration_no text,
    contact         jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE awards (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id   uuid REFERENCES tenders(id),
    supplier_id uuid REFERENCES suppliers(id),
    award_date  timestamptz,
    amount      numeric(18,2),
    currency    char(3) DEFAULT 'ZAR',
    raw         jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- OCDS mirror layer (§10.2.6) — raw releases archived verbatim
-- ---------------------------------------------------------------------------

CREATE TABLE ocds_records (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ocid       text NOT NULL,
    release_id text,
    stage      text,                          -- planning / tender / award / contract
    raw        jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ocid, release_id)
);

CREATE INDEX idx_ocds_records_ocid ON ocds_records(ocid);

-- ---------------------------------------------------------------------------
-- Embeddings (§12) — dimension set for common embedding models; adjust per model
-- ---------------------------------------------------------------------------

CREATE TABLE tender_embeddings (
    tender_id  uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    model      text NOT NULL,
    embedding  vector(1536),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tender_id, model)
);

-- ---------------------------------------------------------------------------
-- Users, companies, alerts (§13, §14)
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text,
    email         text NOT NULL UNIQUE,
    password_hash text,
    role          text NOT NULL DEFAULT 'user',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE companies (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id),
    name        text NOT NULL,
    industry    text,
    regions     jsonb NOT NULL DEFAULT '[]'::jsonb,
    cidb_grades jsonb NOT NULL DEFAULT '[]'::jsonb,
    bbee_level  text
);

CREATE TABLE user_alerts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id),
    keywords        text,
    provinces       jsonb NOT NULL DEFAULT '[]'::jsonb,
    categories      jsonb NOT NULL DEFAULT '[]'::jsonb,
    min_match_score smallint,
    channels        jsonb NOT NULL DEFAULT '["email"]'::jsonb,
    batch_prefs     jsonb NOT NULL DEFAULT '{}'::jsonb,
    active          boolean NOT NULL DEFAULT true
);

CREATE TABLE alert_events (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id   uuid NOT NULL REFERENCES user_alerts(id),
    tender_id  uuid NOT NULL REFERENCES tenders(id),
    channel    text NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now(),
    clicked_at timestamptz
);

-- ---------------------------------------------------------------------------
-- Crawl infrastructure (§5.3)
-- ---------------------------------------------------------------------------

CREATE TABLE crawl_jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid NOT NULL REFERENCES sources(id),
    run_time      timestamptz NOT NULL DEFAULT now(),
    state         crawl_state NOT NULL DEFAULT 'PENDING',
    attempts      integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz,
    error         text
);

CREATE INDEX idx_crawl_jobs_source ON crawl_jobs(source_id);
CREATE INDEX idx_crawl_jobs_state ON crawl_jobs(state);

-- Append-only: old HTML replayable when extraction rules improve (§5.3)
CREATE TABLE crawl_results (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid NOT NULL REFERENCES crawl_jobs(id),
    url              text,
    http_status      smallint,
    etag             text,                    -- conditional fetching (§5.3)
    last_modified    text,
    raw              jsonb NOT NULL DEFAULT '{}'::jsonb,
    extracted_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_crawl_results_job ON crawl_results(job_id);

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crawl_results_append_only
    BEFORE UPDATE OR DELETE ON crawl_results
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE source_health (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id        uuid NOT NULL REFERENCES sources(id),
    checked_at       timestamptz NOT NULL DEFAULT now(),
    last_success     timestamptz,
    last_error       text,
    status_code      smallint,
    mttd_started_at  timestamptz              -- when breakage began (MTTD KPI, §16)
);

CREATE INDEX idx_source_health_source ON source_health(source_id);

-- ---------------------------------------------------------------------------
-- Review queue (§6) & audit (§9)
-- ---------------------------------------------------------------------------

CREATE TABLE review_queue (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id   uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    field       text NOT NULL,                -- e.g. 'closing_at'
    value       jsonb,
    confidence  real,
    queued_at   timestamptz NOT NULL DEFAULT now(),
    reviewed_by uuid REFERENCES users(id),
    resolved_at timestamptz,
    resolution  jsonb                          -- accepted / corrected value
);

CREATE INDEX idx_review_queue_open ON review_queue(tender_id) WHERE resolved_at IS NULL;

CREATE TABLE audit_logs (
    id         bigserial PRIMARY KEY,
    table_name text NOT NULL,
    record_id  uuid,
    action     text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now(),
    user_id    uuid,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- updated_at housekeeping
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER organisations_touch BEFORE UPDATE ON organisations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER sources_touch BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER tenders_touch BEFORE UPDATE ON tenders
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
