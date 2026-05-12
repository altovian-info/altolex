-- ============================================================
-- AltoLex — Supabase setup v4
-- Custom authentication — no dependency on Supabase auth.users
-- Users and roles managed entirely in your own tables.
--
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- ── 1. Firms ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS firms (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name       TEXT NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'starter',  -- starter | pro | enterprise
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 2. Users (replaces Supabase auth.users entirely) ─────────────────────────
-- Passwords hashed with bcrypt in application layer (never stored plain).
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,              -- bcrypt hash, set by app
    full_name       TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'associate',
                                                -- admin | partner | associate | paralegal | readonly
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS users_email_idx   ON users(email);
CREATE INDEX IF NOT EXISTS users_firm_idx    ON users(firm_id);

-- ── 3. Sessions ───────────────────────────────────────────────────────────────
-- Server-side session tokens stored here.
-- Streamlit stores only the token string in session_state.
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,               -- random 64-char hex token
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    ip_address  TEXT
);

CREATE INDEX IF NOT EXISTS sessions_user_idx    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

-- ── 4. Clients ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id    UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    full_name  TEXT NOT NULL,
    email      TEXT,
    phone      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 5. Cases ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cases (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    area_of_law TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── 6. Documents (vector store) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id         BIGSERIAL PRIMARY KEY,
    firm_id    UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    case_id    UUID REFERENCES cases(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    embedding  VECTOR(1024),                    -- voyage-law-2 dimensions
    metadata   JSONB DEFAULT '{}',
    file_hash  TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS documents_firm_idx  ON documents(firm_id);
CREATE INDEX IF NOT EXISTS documents_case_idx  ON documents(case_id);

-- ── 7. Conversations ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    case_id     UUID REFERENCES cases(id) ON DELETE SET NULL,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    doc_sources JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS conversations_firm_idx ON conversations(firm_id);

-- ── 8. Audit log ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    firm_id       UUID NOT NULL,
    user_id       UUID,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    ip_address    TEXT,
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_log_firm_idx    ON audit_log(firm_id);
CREATE INDEX IF NOT EXISTS audit_log_user_idx    ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log(created_at DESC);


-- ════════════════════════════════════════════════════════════
-- VECTOR SEARCH FUNCTION
-- Now uses firm_id from your own users table, not auth.uid()
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
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT
        d.id,
        d.content,
        d.metadata,
        1 - (d.embedding <=> query_embedding) AS similarity
    FROM documents d
    WHERE
        d.firm_id = p_firm_id
        AND (p_case_id IS NULL OR d.case_id = p_case_id OR d.case_id IS NULL)
        AND (filter_doc_type IS NULL OR d.metadata->>'doc_type' = filter_doc_type)
    ORDER BY d.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- ════════════════════════════════════════════════════════════
-- CLEANUP FUNCTION — expire old sessions (run via cron or manually)
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cleanup_expired_sessions()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM sessions WHERE expires_at < NOW();
END;
$$;


-- ════════════════════════════════════════════════════════════
-- SEED: create your first firm and admin user
-- Replace values before running.
-- Password below is bcrypt hash of "changeme123" — CHANGE IT on first login.
-- Generate a new hash at: https://bcrypt.online (cost factor 12)
-- ════════════════════════════════════════════════════════════

-- INSERT INTO firms (id, name, plan)
-- VALUES ('00000000-0000-0000-0000-000000000001', 'Altovian Law', 'pro');

-- INSERT INTO users (firm_id, email, password_hash, full_name, role)
-- VALUES (
--     '00000000-0000-0000-0000-000000000001',
--     'admin@yourfirm.com',
--     '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj/VgYy6K3Hy',  -- "changeme123"
--     'Admin User',
--     'admin'
-- );


-- ════════════════════════════════════════════════════════════
-- MIGRATION FROM v3 (had attorneys table referencing auth.users)
-- ════════════════════════════════════════════════════════════
-- 1. Run this file to create the new tables.
-- 2. Add your firm via the SEED block above (uncomment and edit).
-- 3. Use the AltoLex admin panel (⚙ Admin) to create all users.
-- 4. Old attorneys table can be dropped: DROP TABLE IF EXISTS attorneys CASCADE;
-- 5. Remove SUPABASE_ANON_KEY and SUPABASE_JWT_SECRET from your secrets —
--    they are no longer needed. Keep SUPABASE_SERVICE_KEY for DB access.


-- ════════════════════════════════════════════════════════════
-- POSTGRES RLS — SECOND SAFETY NET
--
-- These policies protect against:
--   1. Direct Supabase dashboard access or SQL mistakes
--   2. Any future code path that bypasses ScopedDB
--   3. Accidental queries from anon/authenticated roles
--
-- Since we use the service role key in application code,
-- these policies do NOT fire for app queries (service role
-- bypasses RLS by design). They are a backstop only.
--
-- To test RLS without service key: use Supabase anon key
-- in a test script — it WILL be blocked by these policies.
-- ════════════════════════════════════════════════════════════

ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases          ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log      ENABLE ROW LEVEL SECURITY;

-- Block ALL access from non-service roles by default
-- (service role bypasses these; application always uses service role)
CREATE POLICY "users: deny non-service"         ON users         FOR ALL USING (false);
CREATE POLICY "sessions: deny non-service"      ON sessions      FOR ALL USING (false);
CREATE POLICY "clients: deny non-service"       ON clients       FOR ALL USING (false);
CREATE POLICY "cases: deny non-service"         ON cases         FOR ALL USING (false);
CREATE POLICY "documents: deny non-service"     ON documents     FOR ALL USING (false);
CREATE POLICY "conversations: deny non-service" ON conversations FOR ALL USING (false);
CREATE POLICY "audit_log: deny non-service"     ON audit_log     FOR ALL USING (false);

-- Result:
--   service role key  → RLS bypassed → full access (controlled by ScopedDB)
--   anon/user key     → RLS applied  → all access blocked
--   Supabase UI       → uses service role → full access (use with care)
