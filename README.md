# AltoLex v4 — Custom authentication

## What changed from v3
| | v3 | v4 |
|---|---|---|
| Auth system | Supabase Auth (auth.users) | Custom users table |
| Login method | Supabase sign_in_with_password | bcrypt password verify |
| Sessions | Supabase JWT | Random token in sessions table |
| User management | Supabase Dashboard only | Built-in admin panel in app |
| Env vars needed | 5 | 4 (removed ANON_KEY, JWT_SECRET) |
| New files | — | auth.py |

## Setup — first time

### 1. Database
Run `supabase_setup_v4.sql` in Supabase SQL Editor.

### 2. Create your first firm + admin user
Uncomment the SEED block at the bottom of the SQL file.
Edit the firm name and email, then run it.

The default password is: changeme123
The admin user MUST change this on first login via ⚙ Admin → edit user.

### 3. Deploy
Add to Streamlit secrets:
  ANTHROPIC_API_KEY
  VOYAGE_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_KEY

### 4. Add more users
Log in as admin → ⚙ Admin → Add User tab.
Set their name, email, role, and a temporary password.
Share the password with them securely — they can change it in the Admin panel.

## Roles
| Role | Intake | Q&A | Document Review | Admin panel |
|---|---|---|---|---|
| admin | ✅ | ✅ | ✅ | ✅ |
| partner | ✅ | ✅ | ✅ | ❌ |
| associate | ✅ | ✅ | ✅ | ❌ |
| paralegal | ✅ | ✅ | ✅ | ❌ |
| readonly | ❌ | ✅ | view only | ❌ |
