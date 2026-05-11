"""
AltoLex — auth.py
Custom authentication using your own users/sessions tables.
No dependency on Supabase Auth whatsoever.

Passwords: bcrypt (cost 12) — never stored plain.
Sessions:  random 64-char hex token stored in Supabase sessions table.
           Streamlit holds only the token in session_state.
"""

import os, secrets
from datetime import datetime, timedelta, timezone
from supabase import create_client
import bcrypt

SESSION_TTL_HOURS = 8   # auto-expire after 8 hours of inactivity


def _svc():
    """Service-role Supabase client — bypasses RLS."""
    url = os.environ.get("SUPABASE_URL") or __import__("streamlit").secrets.get("SUPABASE_URL","")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or __import__("streamlit").secrets.get("SUPABASE_SERVICE_KEY","")
    return create_client(url, key)


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt (cost 12)."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── Login ─────────────────────────────────────────────────────────────────────

def login(email: str, password: str, ip: str = None) -> dict | None:
    """
    Authenticate by email + password against the users table.
    Returns session context dict on success, None on failure.
    """
    sb = _svc()

    # Fetch user row (include password_hash)
    result = sb.table("users") \
               .select("id, firm_id, full_name, role, password_hash, is_active") \
               .eq("email", email.strip().lower()) \
               .single() \
               .execute()

    if not result.data:
        return None

    user = result.data

    if not user.get("is_active"):
        return None

    if not verify_password(password, user["password_hash"]):
        return None

    # Create session token
    token    = secrets.token_hex(32)   # 64-char hex string
    expires  = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    sb.table("sessions").insert({
        "token":      token,
        "user_id":    user["id"],
        "firm_id":    user["firm_id"],
        "expires_at": expires.isoformat(),
        "ip_address": ip,
    }).execute()

    # Update last_login
    sb.table("users").update({"last_login": datetime.now(timezone.utc).isoformat()}) \
                     .eq("id", user["id"]).execute()

    # Write audit log
    sb.table("audit_log").insert({
        "firm_id":    user["firm_id"],
        "user_id":    user["id"],
        "action":     "login",
        "ip_address": ip,
    }).execute()

    return {
        "token":     token,
        "user_id":   user["id"],
        "firm_id":   user["firm_id"],
        "full_name": user["full_name"],
        "role":      user["role"],
        "email":     email.strip().lower(),
    }


# ── Session validation ────────────────────────────────────────────────────────

def validate_session(token: str) -> dict | None:
    """
    Validate a session token. Returns the session context or None if expired/invalid.
    Called on every Streamlit rerun to verify the session is still valid.
    """
    if not token:
        return None

    sb  = _svc()
    now = datetime.now(timezone.utc).isoformat()

    result = sb.table("sessions") \
               .select("user_id, firm_id, expires_at") \
               .eq("token", token) \
               .gt("expires_at", now) \
               .single() \
               .execute()

    if not result.data:
        return None

    sess = result.data

    # Fetch current user details (catches deactivated accounts mid-session)
    user = sb.table("users") \
             .select("full_name, role, email, is_active") \
             .eq("id", sess["user_id"]) \
             .single() \
             .execute()

    if not user.data or not user.data.get("is_active"):
        return None

    return {
        "token":     token,
        "user_id":   sess["user_id"],
        "firm_id":   sess["firm_id"],
        "full_name": user.data["full_name"],
        "role":      user.data["role"],
        "email":     user.data["email"],
    }


# ── Logout ────────────────────────────────────────────────────────────────────

def logout(token: str):
    """Delete the session token from the database."""
    if token:
        _svc().table("sessions").delete().eq("token", token).execute()


# ── User management (admin only) ──────────────────────────────────────────────

def create_user(firm_id: str, email: str, password: str,
                full_name: str, role: str, created_by: str) -> dict:
    """Create a new user. Only callable by admin role (enforced in UI layer)."""
    sb = _svc()

    # Check email not already taken
    existing = sb.table("users").select("id").eq("email", email.strip().lower()).execute()
    if existing.data:
        raise ValueError(f"Email {email} is already registered.")

    row = sb.table("users").insert({
        "firm_id":       firm_id,
        "email":         email.strip().lower(),
        "password_hash": hash_password(password),
        "full_name":     full_name,
        "role":          role,
        "is_active":     True,
        "created_by":    created_by,
    }).execute()

    return row.data[0]


def update_user(user_id: str, updates: dict):
    """Update user fields. Pass password to change it (will be re-hashed)."""
    sb = _svc()
    if "password" in updates:
        updates["password_hash"] = hash_password(updates.pop("password"))
    sb.table("users").update(updates).eq("id", user_id).execute()


def deactivate_user(user_id: str):
    """Deactivate a user (soft delete — preserves audit history)."""
    sb = _svc()
    sb.table("users").update({"is_active": False}).eq("id", user_id).execute()
    # Invalidate all active sessions
    sb.table("sessions").delete().eq("user_id", user_id).execute()


def list_users(firm_id: str) -> list[dict]:
    """List all users for a firm."""
    result = _svc().table("users") \
                   .select("id, email, full_name, role, is_active, last_login, created_at") \
                   .eq("firm_id", firm_id) \
                   .order("created_at") \
                   .execute()
    return result.data or []


def log_action(firm_id: str, user_id: str, action: str, metadata: dict = None):
    """Write to audit_log."""
    try:
        _svc().table("audit_log").insert({
            "firm_id":  firm_id,
            "user_id":  user_id,
            "action":   action,
            "metadata": metadata or {},
        }).execute()
    except Exception:
        pass  # never interrupt user flow for audit failures
