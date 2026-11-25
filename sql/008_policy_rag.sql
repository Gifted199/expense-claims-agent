-- 008: policy handbook RAG (decisions D24-D28).
-- * policy_city_hotel_caps: per-city nightly hotel caps by grade (reference data, single source
--   of truth for the handbook and the Phase 4 workflow). "Elsewhere in the UK" is the existing
--   policy_limits hotel row, so it is not repeated here.
-- * policy_documents: handbook versions; exactly one is_latest.
-- * policy_chunks: structure-aware chunks with one embedding per model, plus full-text vector.
-- * app_ingest: the only role that can write policy_documents / policy_chunks.

CREATE TABLE IF NOT EXISTS policy_city_hotel_caps (
    city         text NOT NULL CHECK (length(city) BETWEEN 1 AND 100),
    grade        text NOT NULL CHECK (grade IN ('associate', 'senior', 'director')),
    nightly_cap  numeric(10,2) NOT NULL CHECK (nightly_cap > 0),
    currency     char(3) NOT NULL DEFAULT 'GBP' CHECK (currency ~ '^[A-Z]{3}$'),
    PRIMARY KEY (city, grade)
);

INSERT INTO policy_city_hotel_caps (city, grade, nightly_cap) VALUES
    ('London',     'associate', 200.00), ('London',     'senior', 230.00), ('London',     'director', 300.00),
    ('Edinburgh',  'associate', 165.00), ('Edinburgh',  'senior', 195.00), ('Edinburgh',  'director', 260.00),
    ('Manchester', 'associate', 140.00), ('Manchester', 'senior', 170.00), ('Manchester', 'director', 240.00)
ON CONFLICT (city, grade) DO NOTHING;

CREATE TABLE IF NOT EXISTS policy_documents (
    version         text PRIMARY KEY CHECK (version ~ '^[0-9]{4}\.[0-9]+$'),
    effective_from  date NOT NULL,
    is_latest       boolean NOT NULL DEFAULT false,
    s3_key          text NOT NULL CHECK (length(s3_key) BETWEEN 1 AND 1024),
    sha256          char(64) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- At most one latest version, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS policy_documents_one_latest
    ON policy_documents (is_latest) WHERE is_latest;

CREATE TABLE IF NOT EXISTS policy_chunks (
    chunk_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version          text NOT NULL REFERENCES policy_documents (version) ON DELETE CASCADE,
    is_latest        boolean NOT NULL,  -- copy of policy_documents.is_latest, kept in sync by ingest
    section_id       text NOT NULL CHECK (section_id ~ '^EXP-[0-9]+(\.[0-9]+)?$'),
    section_path     text NOT NULL,
    title            text NOT NULL,
    text             text NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
    chunk_index      smallint NOT NULL DEFAULT 0 CHECK (chunk_index >= 0),
    embedding_model  text NOT NULL CHECK (length(embedding_model) BETWEEN 1 AND 100),
    embedding        vector(1536) NOT NULL,
    tsv              tsvector GENERATED ALWAYS AS (
                         setweight(to_tsvector('simple'::regconfig, section_id), 'A')
                         || setweight(to_tsvector('english'::regconfig, title), 'B')
                         || to_tsvector('english'::regconfig, text)
                     ) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version, section_id, chunk_index, embedding_model)
);
CREATE INDEX IF NOT EXISTS policy_chunks_embedding_hnsw
    ON policy_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS policy_chunks_tsv_gin ON policy_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS policy_chunks_model_latest_idx
    ON policy_chunks (embedding_model, is_latest);

-- Roles and grants -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ingest') THEN
        CREATE ROLE app_ingest LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_ingest', current_database());
END;
$$;

GRANT USAGE ON SCHEMA public TO app_ingest;
-- Reads only what the handbook is generated from.
GRANT SELECT ON policy_limits, expense_categories, policy_city_hotel_caps TO app_ingest;
-- Writes only the RAG tables (re-ingest replaces a version/model's chunks, hence DELETE).
GRANT SELECT, INSERT, UPDATE, DELETE ON policy_documents, policy_chunks TO app_ingest;
GRANT USAGE ON SEQUENCE policy_chunks_chunk_id_seq TO app_ingest;
ALTER ROLE app_ingest SET statement_timeout = '120s';

-- search_policy (Phase 4) reads as app_readonly; the workflow reads city caps as app_writer.
GRANT SELECT ON policy_documents, policy_chunks, policy_city_hotel_caps TO app_readonly;
GRANT SELECT ON policy_city_hotel_caps TO app_writer;
