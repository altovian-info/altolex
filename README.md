# AltoLex v4b — Scoped DB + RLS safety net

## What changed from v4

| | v4 | v4b |
|---|---|---|
| Tenant isolation | firm_id filters in queries | ScopedDB wrapper — structural enforcement |
| Cross-tenant protection | Manual, forgettable | Impossible — ScopedDB raises PermissionError |
| RLS | Disabled | Deny-all for non-service roles (backstop) |
| auth.py queries | Mixed raw/scoped | All writes via ScopedDB |
| rag_v3.py | Used SUPABASE_ANON_KEY (dead) | Removed, uses ScopedDB.rpc() |
| New files | — | db.py |

## Security model (two layers)

Layer 1 — ScopedDB (application):
  Every query goes through ScopedDB(firm_id).
  firm_id is injected automatically on SELECT/INSERT/UPDATE/DELETE.
  RPC calls verify p_firm_id matches the scoped firm_id.
  A bug that forgets to filter by firm_id is structurally prevented.

Layer 2 — Postgres RLS (database):
  Deny-all policies on all tenant tables for non-service roles.
  Service role (used by app) bypasses RLS — controlled by Layer 1.
  Anon/user key access is blocked at DB level.
  Protects against accidental direct DB access or leaked anon key.

## Setup
Same as v4. Run supabase_setup_v4.sql (includes RLS policies at bottom).
Deploy app_v4.py — it imports from auth.py which imports from db.py.
