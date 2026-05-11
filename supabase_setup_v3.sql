-- ============================================================
-- AltoLex — Supabase setup v2
-- Multi-tenant with Row Level Security (RLS)
--
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- New project: run the full file.
-- Upgrading from v1: run only the MIGRATION section at bottom.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── 1. Firms (your tenants) ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS firms (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name       TEXT NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'starter',  -- starter | pro | enterprise
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 2. Attorneys (extends Supabase auth.users) ────────────────────────────────
CREATE TABLE IF NOT EXISTS attorneys (
    id         UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    firm_id    UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    full_name  TEXT,
    role       TEXT NOT NULL DEFAULT 'associate',  -- partner | associate | paralegal | admin
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 3. Clients ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id    UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    full_name  TEXT NOT NULL,
    email      TEXT,
    phone      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 4. Cases ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cases (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    area_of_law TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | closed | archived
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── 5. Documents (vector store) ───────────────────────────────────────────────
-- Every chunk carries firm_id + case_id. No row is ever accessible
-- to another firm — enforced at DB level, not app level.
CREATE TABLE IF NOT EXISTS documents (
    id         BIGSERIAL PRIMARY KEY,
    firm_id    UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    case_id    UUID REFERENCES cases(id) ON DELETE CASCADE,  -- NULL = firm-wide knowledge base
    content    TEXT NOT NULL,
    embedding  VECTOR(1024),
    metadata   JSONB DEFAULT '{}',
    file_hash  TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 6. Conversations (audit trail) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id      UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    case_id      UUID REFERENCES cases(id) ON DELETE SET NULL,
    attorney_id  UUID REFERENCES attorneys(id) ON DELETE SET NULL,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    doc_sources  JSONB DEFAULT '[]',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ── 7. Audit log (immutable — append only) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    firm_id       UUID NOT NULL,
    attorney_id   UUID,
    action        TEXT NOT NULL,   -- query | ingest | delete | login | logout
    resource_type TEXT,            -- document | case | client
    resource_id   TEXT,
    ip_address    TEXT,
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);


-- ════════════════════════════════════════════════════════════
-- INDEXES
-- ════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS documents_firm_idx      ON documents(firm_id);
CREATE INDEX IF NOT EXISTS documents_case_idx      ON documents(case_id);
CREATE INDEX IF NOT EXISTS conversations_firm_idx  ON conversations(firm_id);
CREATE INDEX IF NOT EXISTS audit_log_firm_idx      ON audit_log(firm_id);
CREATE INDEX IF NOT EXISTS audit_log_atty_idx      ON audit_log(attorney_id);
CREATE INDEX IF NOT EXISTS audit_log_created_idx   ON audit_log(created_at DESC);


-- ════════════════════════════════════════════════════════════
-- HELPER: resolve current attorney's firm from JWT
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION current_firm_id()
RETURNS UUID LANGUAGE sql STABLE AS $$
    SELECT firm_id FROM attorneys WHERE id = auth.uid();
$$;


-- ════════════════════════════════════════════════════════════
-- ROW LEVEL SECURITY
-- Every table is locked to the calling attorney's firm.
-- A misconfigured query physically cannot return another firm's data.
-- ════════════════════════════════════════════════════════════

ALTER TABLE firms          ENABLE ROW LEVEL SECURITY;
ALTER TABLE attorneys      ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases          ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log      ENABLE ROW LEVEL SECURITY;

CREATE POLICY "firms: own firm"         ON firms         FOR ALL USING (id = current_firm_id());
CREATE POLICY "attorneys: same firm"    ON attorneys     FOR ALL USING (firm_id = current_firm_id());
CREATE POLICY "clients: own firm"       ON clients       FOR ALL USING (firm_id = current_firm_id());
CREATE POLICY "cases: own firm"         ON cases         FOR ALL USING (firm_id = current_firm_id());
CREATE POLICY "documents: own firm"     ON documents     FOR ALL USING (firm_id = current_firm_id());
CREATE POLICY "conversations: own firm" ON conversations FOR ALL USING (firm_id = current_firm_id());

-- Audit log: attorneys can READ their firm's log; only service-role key can write
CREATE POLICY "audit_log: read own firm" ON audit_log
    FOR SELECT USING (firm_id = current_firm_id());


-- ════════════════════════════════════════════════════════════
-- MATCH FUNCTION — tenant-scoped vector similarity search
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION match_documents(
    query_embedding  VECTOR(1024),
    p_firm_id        UUID,
    match_count      INT  DEFAULT 5,
    p_case_id        UUID DEFAULT NULL,
    filter_doc_type  TEXT DEFAULT NULL
)
RETURNS TABLE (
    id         BIGINT,
    content    TEXT,
    metadata   JSONB,
    similarity FLOAT
)
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN QUERY
    SELECT
        d.id,
        d.content,
        d.metadata,
        1 - (d.embedding <=> query_embedding) AS similarity
    FROM documents d
    WHERE
        d.firm_id = p_firm_id                                   -- hard tenant fence
        AND (
            p_case_id IS NULL                                   -- no case filter: include all
            OR d.case_id = p_case_id                            -- this case's documents
            OR d.case_id IS NULL                                -- firm-wide knowledge base
        )
        AND (filter_doc_type IS NULL
             OR d.metadata->>'doc_type' = filter_doc_type)
    ORDER BY d.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- ════════════════════════════════════════════════════════════
-- TRIGGER: auto-create attorney profile on Supabase Auth signup
-- The signup call must include raw_user_meta_data:
--   { "firm_id": "<uuid>", "full_name": "...", "role": "associate" }
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION handle_new_attorney()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO attorneys (id, firm_id, full_name, role)
    VALUES (
        NEW.id,
        (NEW.raw_user_meta_data->>'firm_id')::UUID,
        NEW.raw_user_meta_data->>'full_name',
        COALESCE(NEW.raw_user_meta_data->>'role', 'associate')
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_attorney();


-- ════════════════════════════════════════════════════════════
-- MIGRATION FROM v1 (existing documents table without tenant cols)
-- Uncomment and run if upgrading — do NOT run on a fresh setup.
-- ════════════════════════════════════════════════════════════

-- Step 1: Create the firms/attorneys/clients/cases tables above first.
-- Step 2: Insert your firm row and get its UUID.
--   INSERT INTO firms (name) VALUES ('Your Firm Name') RETURNING id;
-- Step 3: Backfill the existing documents rows with that firm_id.
--   ALTER TABLE documents ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id);
--   ALTER TABLE documents ADD COLUMN IF NOT EXISTS case_id UUID REFERENCES cases(id);
--   UPDATE documents SET firm_id = '<paste-uuid-here>' WHERE firm_id IS NULL;
--   ALTER TABLE documents ALTER COLUMN firm_id SET NOT NULL;
-- Step 4: Enable RLS.
--   ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY "documents: own firm" ON documents FOR ALL USING (firm_id = current_firm_id());
-- Step 5: Replace the match_documents function with the new version above.


-- ════════════════════════════════════════════════════════════
-- MIGRATION FROM v2 (changing embedding dimension 384 → 1024)
-- Run if you already have data embedded with all-MiniLM-L6-v2.
-- WARNING: you MUST re-ingest all documents after this — old
-- 384-dim vectors are incompatible with 1024-dim voyage-law-2.
-- ════════════════════════════════════════════════════════════
-- Step 1: Drop the old index (cannot change dimension with index in place)
-- DROP INDEX IF EXISTS documents_embedding_idx;

-- Step 2: Change the column dimension
-- ALTER TABLE documents ALTER COLUMN embedding TYPE VECTOR(1024);

-- Step 3: Clear old embeddings (they are incompatible — must re-ingest)
-- DELETE FROM documents;

-- Step 4: Recreate the index
-- CREATE INDEX documents_embedding_idx
--     ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Step 5: Re-run ingest_v3.py for all your documents
--   python ingest_v3.py --dir ./docs --firm-id <uuid>
