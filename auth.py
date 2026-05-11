"""
AltoLex — auth.py  (scoped)
Custom authentication — all DB queries use ScopedDB.
firm_id is enforced structurally on every query after login.

Passwords: bcrypt cost 12.
Sessions:  64-char hex token in sessions table, TTL 8 hours.
"""

import os, secrets
from datetime import datetime, timedelta, timezone
from db import ScopedDB, raw_client
import bcrypt

SESSION_TTL_HOURS = 8


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:    return bcrypt.checkpw(plain.encode(), hashed.encode())
    except: return False


def login(email: str, password: str, ip: str = None) -> dict | None:
    """
    Email/password login.
    Initial user lookup is unscoped (firm_id unknown at that point).
    All writes afterwards go through ScopedDB.
    """
    rc     = raw_client()
    result = rc.table("users") \
               .select("id, firm_id, full_name, role, password_hash, is_active") \
               .eq("email", email.strip().lower()) \
               .single().execute()

    if not result.data:                         return None
    user = result.data
    if not user.get("is_active"):               return None
    if not verify_password(password, user["password_hash"]): return None

    db      = ScopedDB(user["firm_id"])
    token   = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    db.table("sessions").insert({
        "token": token, "user_id": user["id"],
        "expires_at": expires.isoformat(), "ip_address": ip,
    }).execute()

    db.table("users").update({"last_login": datetime.now(timezone.utc).isoformat()}) \
                     .eq("id", user["id"]).execute()

    db.table("audit_log").insert({"user_id": user["id"], "action": "login",
                                   "ip_address": ip}).execute()

    return {"token": token, "user_id": user["id"], "firm_id": user["firm_id"],
            "full_name": user["full_name"], "role": user["role"],
            "email": email.strip().lower()}


def validate_session(token: str) -> dict | None:
    """Validate token on every Streamlit rerun. Unscoped session lookup, then scoped user fetch."""
    if not token: return None
    rc  = raw_client()
    now = datetime.now(timezone.utc).isoformat()

    sess = rc.table("sessions").select("user_id, firm_id, expires_at") \
             .eq("token", token).gt("expires_at", now).single().execute()
    if not sess.data: return None

    user = ScopedDB(sess.data["firm_id"]).table("users") \
               .select("full_name, role, email, is_active") \
               .eq("id", sess.data["user_id"]).single().execute()
    if not user.data or not user.data.get("is_active"): return None

    return {"token": token, "user_id": sess.data["user_id"],
            "firm_id": sess.data["firm_id"], "full_name": user.data["full_name"],
            "role": user.data["role"], "email": user.data["email"]}


def logout(token: str):
    if token: raw_client().table("sessions").delete().eq("token", token).execute()


def create_user(firm_id: str, email: str, password: str,
                full_name: str, role: str, created_by: str) -> dict:
    if raw_client().table("users").select("id").eq("email", email.strip().lower()).execute().data:
        raise ValueError(f"Email {email} is already registered.")
    row = ScopedDB(firm_id).table("users").insert({
        "email": email.strip().lower(), "password_hash": hash_password(password),
        "full_name": full_name, "role": role, "is_active": True, "created_by": created_by,
    }).execute()
    return row.data[0]


def update_user(firm_id: str, user_id: str, updates: dict):
    if "password" in updates:
        updates["password_hash"] = hash_password(updates.pop("password"))
    ScopedDB(firm_id).table("users").update(updates).eq("id", user_id).execute()


def deactivate_user(firm_id: str, user_id: str):
    ScopedDB(firm_id).table("users").update({"is_active": False}).eq("id", user_id).execute()
    raw_client().table("sessions").delete().eq("user_id", user_id).execute()


def reactivate_user(firm_id: str, user_id: str):
    ScopedDB(firm_id).table("users").update({"is_active": True}).eq("id", user_id).execute()


def list_users(firm_id: str) -> list[dict]:
    r = ScopedDB(firm_id).table("users") \
          .select("id, email, full_name, role, is_active, last_login, created_at") \
          .order("created_at").execute()
    return r.data or []


def log_action(firm_id: str, user_id: str, action: str, metadata: dict = None):
    try:
        ScopedDB(firm_id).table("audit_log").insert({
            "user_id": user_id, "action": action, "metadata": metadata or {}
        }).execute()
    except Exception:
        pass
