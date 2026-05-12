"""
AltoLex — db.py
ScopedDB: a thin wrapper around the Supabase service client that makes
it structurally impossible to query user data without a firm_id filter.

Why:
  - We use the service role key (bypasses Supabase RLS)
  - Therefore isolation MUST be enforced at application layer
  - This class is the single choke point — every data query goes through it
  - Postgres RLS policies are added as a second safety net (see SQL file)

Usage:
    db = ScopedDB(firm_id)
    db.table("documents").select("*").execute()
    # → automatically adds .eq("firm_id", firm_id) to every query
"""

import os
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions


def _raw_client() -> Client:
    import streamlit as st

    url = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY")
        or st.secrets.get("SUPABASE_SERVICE_KEY", "")
        or st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")  # common alternative name
        or st.secrets.get("supabase_service_key", "")       # lowercase variant
    )

    if not url:
        raise ValueError("SUPABASE_URL is not set in secrets or environment.")
    if not key:
        raise ValueError(
            "Service key not found. Check Streamlit Secrets — "
            "the key must be named exactly: SUPABASE_SERVICE_KEY"
        )
    return create_client(url, key)


# Tables that carry firm_id and must always be scoped
TENANT_TABLES = {"users", "documents", "clients", "cases", "conversations", "audit_log", "sessions"}

# Tables that are NOT scoped (lookup/reference tables)
UNSCOPED_TABLES = {"firms"}


class _ScopedQueryBuilder:
    """
    Wraps a Supabase query builder and injects .eq("firm_id", firm_id)
    on every SELECT, UPDATE, and DELETE automatically.
    INSERT rows are validated to contain firm_id before execution.
    """

    def __init__(self, builder, firm_id: str, table: str, operation: str):
        self._b         = builder
        self._firm_id   = firm_id
        self._table     = table
        self._operation = operation
        self._scoped    = False

        # Auto-scope reads and mutations on tenant tables
        if table in TENANT_TABLES and operation in ("select", "update", "delete"):
            self._b = self._b.eq("firm_id", firm_id)
            self._scoped = True

    def __getattr__(self, name):
        # Proxy all other query builder methods (select, eq, order, limit, single, etc.)
        attr = getattr(self._b, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                # If the result is still a query builder, keep wrapping
                if hasattr(result, "execute"):
                    self._b = result
                    return self
                return result
            return wrapper
        return attr

    def execute(self):
        return self._b.execute()


class ScopedDB:
    """
    Tenant-isolated database client.
    All queries are automatically scoped to the provided firm_id.

    Usage:
        db = ScopedDB(firm_id="...")
        rows = db.table("documents").select("content, metadata").execute()
        # firm_id filter is injected automatically — no other firm's data returned
    """

    def __init__(self, firm_id: str):
        if not firm_id:
            raise ValueError("ScopedDB requires a firm_id. Never instantiate without one.")
        self._firm_id = str(firm_id)
        self._client  = _raw_client()

    def table(self, name: str):
        return _ScopedTable(self._client, self._firm_id, name)

    def rpc(self, fn: str, params: dict):
        """
        For RPC calls (e.g. match_documents), firm_id must be in params.
        This wrapper enforces that p_firm_id is always set to the scoped firm_id,
        preventing any caller from passing a different firm_id.
        """
        if "p_firm_id" in params and params["p_firm_id"] != self._firm_id:
            raise PermissionError(
                f"ScopedDB.rpc: p_firm_id mismatch. "
                f"Got {params['p_firm_id']}, expected {self._firm_id}. "
                f"Cross-tenant RPC calls are not permitted."
            )
        # Always overwrite — even if caller passed wrong value, we correct it
        params["p_firm_id"] = self._firm_id
        return self._client.rpc(fn, params)


class _ScopedTable:
    """Returned by ScopedDB.table() — scopes all operations to firm_id."""

    def __init__(self, client: Client, firm_id: str, name: str):
        self._client   = client
        self._firm_id  = firm_id
        self._name     = name
        self._is_tenant = name in TENANT_TABLES

    def select(self, columns: str = "*"):
        builder = self._client.table(self._name).select(columns)
        if self._is_tenant:
            builder = builder.eq("firm_id", self._firm_id)
        return builder

    def insert(self, data: dict | list):
        """Validate and inject firm_id into every row before insert."""
        rows = data if isinstance(data, list) else [data]
        if self._is_tenant:
            for row in rows:
                if "firm_id" not in row:
                    row["firm_id"] = self._firm_id
                elif row["firm_id"] != self._firm_id:
                    raise PermissionError(
                        f"ScopedDB insert: firm_id mismatch on table '{self._name}'. "
                        f"Got {row['firm_id']}, expected {self._firm_id}. Blocked."
                    )
        payload = rows if isinstance(data, list) else rows[0]
        return self._client.table(self._name).insert(payload)

    def update(self, data: dict):
        builder = self._client.table(self._name).update(data)
        if self._is_tenant:
            builder = builder.eq("firm_id", self._firm_id)
        return builder

    def delete(self):
        builder = self._client.table(self._name).delete()
        if self._is_tenant:
            builder = builder.eq("firm_id", self._firm_id)
        return builder

    def upsert(self, data: dict | list):
        rows = data if isinstance(data, list) else [data]
        if self._is_tenant:
            for row in rows:
                row["firm_id"] = self._firm_id
        payload = rows if isinstance(data, list) else rows[0]
        return self._client.table(self._name).upsert(payload)


def raw_client() -> Client:
    """
    Returns the unscoped service client.
    ONLY use for:
      - Session token lookups (sessions table, keyed by token not firm_id)
      - Cross-firm admin operations (none currently)
    Document this whenever used.
    """
    return _raw_client()
