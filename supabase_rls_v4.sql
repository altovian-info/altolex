-- ============================================================
-- AltoLex — RLS for custom auth (v4)
-- Run this AFTER supabase_setup_v4.sql
--
-- How it works:
-- The app sets a Postgres session variable (app.current_firm_id)
-- before each query using a SET LOCAL call.
-- RLS policies read this variable instead of auth.uid().
-- The service key is still used — but now the DB enforces
-- tenant isolation as a second layer, not just the app.
-- ============================================================

-- ── Helper function — reads the session variable set by the app ───────────────
CREATE OR REPLACE FUNCTION app_firm_id()
RETURNS UUID LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_firm_id', TRUE), '')::UUID;
$$;


-- ── Enable RLS on all data tables ─────────────────────────────────────────────
ALTER TABLE firms          ENABLE ROW LEVEL SECURITY;
ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases          ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log      ENABLE ROW LEVEL SECURITY;


-- ── Policies — every table scoped to app_firm_id() ───────────────────────────

-- Firms: only see your own firm row
CREATE POLICY "firms: own firm" ON firms
    FOR ALL USING (id = app_firm_id());

-- Users: only see users in your firm
CREATE POLICY "users: own firm" ON users
    FOR ALL USING (firm_id = app_firm_id());

-- Sessions: only see sessions in your firm
CREATE POLICY "sessions: own firm" ON sessions
    FOR ALL USING (firm_id = app_firm_id());

-- Clients: own firm only
CREATE POLICY "clients: own firm" ON clients
    FOR ALL USING (firm_id = app_firm_id());

-- Cases: own firm only
CREATE POLICY "cases: own firm" ON cases
    FOR ALL USING (firm_id = app_firm_id());

-- Documents: own firm only
CREATE POLICY "documents: own firm" ON documents
    FOR ALL USING (firm_id = app_firm_id());

-- Conversations: own firm only
CREATE POLICY "conversations: own firm" ON conversations
    FOR ALL USING (firm_id = app_firm_id());

-- Audit log: own firm only (read); service role writes via bypass
CREATE POLICY "audit_log: own firm" ON audit_log
    FOR ALL USING (firm_id = app_firm_id());


-- ── IMPORTANT: login and session validation need to bypass RLS ────────────────
-- The login flow must read users by email before firm_id is known.
-- We handle this with a SECURITY DEFINER function that bypasses RLS
-- for the specific purpose of credential verification only.

CREATE OR REPLACE FUNCTION verify_credentials(p_email TEXT)
RETURNS TABLE (
    id            UUID,
    firm_id       UUID,
    password_hash TEXT,
    full_name     TEXT,
    role          TEXT,
    is_active     BOOLEAN
)
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    -- This function bypasses RLS intentionally — it's the only way
    -- to look up a user by email before we know their firm_id.
    -- It returns ONLY the fields needed for auth — never full user data.
    RETURN QUERY
    SELECT u.id, u.firm_id, u.password_hash, u.full_name, u.role, u.is_active
    FROM users u
    WHERE u.email = lower(p_email);
END;
$$;

-- Similarly for session validation
CREATE OR REPLACE FUNCTION validate_session_token(p_token TEXT)
RETURNS TABLE (
    user_id    UUID,
    firm_id    UUID,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN QUERY
    SELECT s.user_id, s.firm_id, s.expires_at
    FROM sessions s
    WHERE s.token = p_token
      AND s.expires_at > NOW();
END;
$$;


-- ── set_config_wrapper — called by Python client to activate RLS ──────────────
-- Supabase Python SDK cannot call SET LOCAL directly.
-- The app calls this RPC before every scoped query.
CREATE OR REPLACE FUNCTION set_config_wrapper(
    setting_name  TEXT,
    setting_value TEXT,
    is_local      BOOLEAN DEFAULT TRUE
)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config(setting_name, setting_value, is_local);
END;
$$;
