# NERVE — Auth Foundation (SP1 of the multi-tenant program)

**Date:** 2026-06-10
**Status:** Approved design — ready for implementation plan
**Author:** brainstormed with the user

---

## 1. Purpose

Add a real authentication system to NERVE: user accounts (email + password),
login/logout, and gating so the dashboard and data APIs require a logged-in user.
This is **SP1** of a 6-part program to turn single-instance NERVE into a
multi-tenant SaaS. SP1 delivers auth + access gating **only** — it deliberately
does not yet scope data per-user (that is SP2).

### The larger program (context, not built here)
```
SP1  Auth foundation          ← THIS SPEC
SP2  Per-user data scoping     user_id on missions; reads filtered by user
SP3  Per-user settings         DB-backed config; app reads the owner's config
SP4  Settings page UI          forms for integrations + Connect Telegram
SP5  Per-user Telegram         route notifications to each user's chat
SP6  Incident goes live        INCIDENT missions run the real workflow
```

## 2. Scope

### In scope (SP1)
- `User` model + users collection (unique email).
- Password hashing (passlib bcrypt) and JWT issuance/verification (pyjwt).
- Auth routes: signup, login, logout, me.
- A JWT httpOnly cookie as the session.
- Route gating via middleware (public allowlist; everything else requires auth).
- A NERVE-themed login/signup page.
- The dashboard fetch layer redirects to `/login` on a 401.
- Unit tests.

### Out of scope (later sub-projects)
- Per-user data isolation — after SP1, every logged-in user still sees the one
  shared mission list (SP2 scopes it).
- Per-user settings / integrations / Telegram (SP3–SP5).
- Password reset, email verification, OAuth, roles/admin (not needed yet; YAGNI).
- Incident-goes-live wiring (SP6).

## 3. Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Multi-tenancy target | Per-user (true multi-tenant) — but SP1 is auth only |
| Signup policy | Open self-serve signup (anyone can create an account) |
| Session mechanism | JWT in an httpOnly, Secure, SameSite=Lax cookie (stateless) |
| Password hashing | passlib bcrypt |
| Token library | pyjwt |

## 4. Architecture

### 4.1 New `auth/` package
- `auth/passwords.py` — `hash_password(plain) -> str`, `verify_password(plain, hash) -> bool` (passlib `CryptContext`, bcrypt).
- `auth/tokens.py` — `create_access_token(user_id, *, expires_minutes=None) -> str` and `decode_token(token) -> str | None` (returns the `sub`/user_id, or None on invalid/expired). Signed with `settings.jwt_secret` (HS256). Constants for the cookie name and claims.
- `auth/dependencies.py` — `async current_user(request) -> User`: reads the JWT cookie, decodes, loads the user from the state layer; raises `HTTPException(401)` if absent/invalid. Also `current_user_optional` returning `User | None`.
- `auth/middleware.py` — an ASGI/HTTP middleware that, for any request **not** matching the public allowlist, requires a valid auth cookie. On failure: HTML/page requests get a 302 redirect to `/login`; API/JSON/WebSocket requests get 401. Public allowlist (prefixes): `/auth`, `/login`, `/health`, `/healthz`, `/webhooks`, `/showcase`, `/live-classic`, `/favicon.ico`, plus OpenAPI/docs if present. Everything else (`/`, `/live`, `/missions`, `/actions`, `/failure`, `/demo`, `/ws`, `/internal`) is gated.

### 4.2 State layer (`state/models.py`, `state/database.py`)
- `User(_BaseDoc)` — `user_id: str (uuid)`, `email: str`, `password_hash: str`, `created_at`. (No `updated_at` needed.)
- `get_users_collection()`; `create_user(email, password_hash) -> User` (lowercases/normalizes email; relies on a **unique index** on `email` to reject dupes → raise a typed `ValueError`/`AuthError` on duplicate); `get_user_by_email(email) -> User | None`; `get_user(user_id) -> User | None`.
- `ensure_indexes()` adds a unique index on `users.email`.

### 4.3 Routes (`routes/auth.py`, prefix `/auth`)
- `POST /auth/signup` — body `{email, password}`. Validates email format + password length (≥ 8). Creates the user (409 on duplicate). Sets the JWT cookie. Returns `{user_id, email}`.
- `POST /auth/login` — body `{email, password}`. Verifies; on success sets the cookie and returns the user; on failure → 401.
- `POST /auth/logout` — clears the cookie. 204.
- `GET /auth/me` — returns the current user (via `current_user`) or 401. Used by the frontend to check session state.
- Cookie attributes: `httponly=True`, `secure=True`, `samesite="lax"`, `max_age=jwt_expire_minutes*60`, `path="/"`. (Secure is fine on Cloud Run HTTPS; for local http the cookie still works because browsers allow Secure cookies on `localhost`.)

### 4.4 Config (`config.py`, `.env.example`)
- `jwt_secret: str = ""` — HS256 signing key. If empty, the app generates an ephemeral random secret at startup and logs a warning (sessions won't survive restarts/instances — fine for dev; production should set it). `.env.example` documents generating one via `openssl rand -hex 32`.
- `jwt_expire_minutes: int = 10080` (7 days).

### 4.5 Dashboard (`routes/dashboard.py`, templates)
- New public `GET /login` → serves `dashboard/templates/login.html`.
- `dashboard/templates/login.html` — a self-contained NERVE-themed page: a single card with email + password, a Login / Sign up toggle, posts via `fetch` to `/auth/login` or `/auth/signup`, shows inline errors, redirects to `/` on success.
- `/` and `/live` remain `FileResponse` routes; the middleware gates them (redirect to `/login` when unauthenticated) — no per-route change needed beyond the middleware.

### 4.6 Frontend session handling
- In the live dashboard adapter (`dashboard/showcase_src/live-data.jsx` `Api`), wrap fetches so a `401` response triggers `location.href = "/login"`. Minimal: a helper that checks `res.status === 401`. (Page navigations are already handled by the middleware redirect.)

### 4.7 Wiring (`main.py`)
- Register the auth router and add the auth middleware (after CORS/logging middleware, before the routers). The middleware reads `request.cookies`.

### 4.8 Dependencies
- `requirements.txt` += `passlib[bcrypt]`, `pyjwt`.

## 5. Data flow
```
GET /            → middleware: no cookie → 302 /login
POST /auth/signup → create user → Set-Cookie(JWT) → 200
(browser redirects to /)
GET /            → middleware: valid cookie → serve live dashboard
GET /missions/X  → middleware: valid cookie → 200 (data)
(cookie expires) GET /missions/X → 401 → frontend → location=/login
```

## 6. Error handling
- Duplicate email → 409. Invalid credentials → 401. Password < 8 chars or bad email → 400. Missing/expired/invalid token → cleared cookie + 302 `/login` (pages) or 401 (APIs). Bcrypt/JWT errors caught and logged at the boundary; never leak internals. Typed exceptions + structlog throughout.

## 7. Testing
Unit + FastAPI `TestClient`, using the existing `mock_db` fixture:
- `passwords`: hash ≠ plain; verify true for right password, false for wrong.
- `tokens`: round-trip create→decode returns the user_id; tampered/expired token → None.
- `database`: create_user persists; duplicate email rejected; get_by_email/get_user.
- `routes`: signup creates a user + sets a cookie; login good→cookie, bad→401; `/auth/me` 200 with cookie, 401 without; signup duplicate→409; weak password→400.
- `gating`: a protected API (`/missions/<id>` or `/auth/me`) returns 401 without a cookie and works with one; a public path (`/health`, `/showcase`) works without a cookie.
- All existing tests must still pass (the middleware must not break the public `/health`, `/webhooks`, and test routes; tests that hit gated routes get an authenticated TestClient helper or call with a cookie).

> Note: existing route tests that hit `/missions`, `/actions`, etc. will now be gated. The test suite gets a small `auth_client` fixture (a TestClient with a valid cookie for a seeded user) and those tests switch to it. This is part of SP1's work.

## 8. Security notes
- Passwords only ever stored as bcrypt hashes. JWT signed (HS256) with `jwt_secret`; httpOnly cookie prevents JS theft; SameSite=Lax mitigates CSRF for top-level navigations (state-changing POSTs are JSON via fetch, same-origin).
- The Dynatrace webhook stays public but is authenticated by its existing shared-secret signature — unchanged.
- `jwt_secret` must be set in production (else sessions are per-instance/ephemeral). Documented.

## 9. Follow-ups (not blocking SP1)
- SP2 scopes missions to `user_id` (attach owner on create; filter all reads).
- Add `jwt-secret` to the Cloud Run secrets/env for durable sessions.
