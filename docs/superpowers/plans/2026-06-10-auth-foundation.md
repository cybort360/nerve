# Auth Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add email/password accounts, JWT-cookie sessions, and route gating so NERVE's dashboard and data APIs require a logged-in user (SP1 of the multi-tenant program; no per-user data scoping yet).

**Architecture:** A stateless JWT in an httpOnly cookie is the session. A small `auth/` package holds password hashing (passlib bcrypt), token create/decode (pyjwt), a `current_user` dependency, and an HTTP middleware that gates every request except a public allowlist. A `User` model + `users` collection live in the existing state layer. A NERVE-themed `/login` page posts to `/auth/*` routes.

**Tech Stack:** Python 3.11+, FastAPI, Motor/MongoDB, Pydantic v2, structlog, passlib[bcrypt], pyjwt, pytest (asyncio_mode=auto), httpx (already present).

---

## File structure

| File | Responsibility | New/Mod |
|---|---|---|
| `requirements.txt` | add passlib[bcrypt], pyjwt | Mod |
| `config.py` | `jwt_secret`, `jwt_expire_minutes` | Mod |
| `.env.example` | document JWT_SECRET | Mod |
| `tests/conftest.py` | set a deterministic JWT_SECRET for tests | Mod |
| `exceptions.py` | `AuthError` | Mod |
| `auth/__init__.py` | package marker | New |
| `auth/passwords.py` | hash/verify passwords | New |
| `auth/tokens.py` | create/decode JWT; cookie name | New |
| `auth/dependencies.py` | `current_user` FastAPI dependency | New |
| `auth/middleware.py` | gate requests outside the public allowlist | New |
| `state/models.py` | `User` model | Mod |
| `state/database.py` | users collection + CRUD + index | Mod |
| `routes/schemas.py` | `SignupRequest`, `LoginRequest`, `UserResponse` | Mod |
| `routes/auth.py` | signup/login/logout/me | New |
| `dashboard/templates/login.html` | login + signup page | New |
| `routes/dashboard.py` | `GET /login` (public) | Mod |
| `main.py` | register auth router + middleware | Mod |
| `dashboard/showcase_src/live-data.jsx` | redirect to /login on 401 | Mod |
| `tests/unit/test_auth.py` | passwords/tokens/users/routes | New |
| `tests/unit/test_auth_middleware.py` | gating via a minimal ASGI app | New |

> **Patterns to follow:** models mirror `Action` (`_BaseDoc`, `Field(default_factory=...)`); db functions mirror `create_action`/`get_actions_for_mission` (use `_execute_write`/`_execute_read`, `error_ctx`, structlog); route handlers are tested by calling them directly with a fake `Request` (see `tests/integration/test_api.py`), not via TestClient. Run tests with `PYTHONPATH=. venv/bin/python -m pytest -q`.

---

### Task 1: Dependencies, config, conftest

**Files:** Modify `requirements.txt`, `config.py`, `.env.example`, `tests/conftest.py`

- [ ] **Step 1: Add dependencies**

Append to `requirements.txt`:
```
passlib[bcrypt]>=1.7,<2.0
pyjwt>=2.8,<3.0
```
Install: `venv/bin/pip install "passlib[bcrypt]>=1.7,<2.0" "pyjwt>=2.8,<3.0"`

- [ ] **Step 2: Add config fields**

In `config.py`, in the `Settings` class, after the Feature Flags block (or near the end), add:
```python
    # --- Auth (JWT session) ---
    jwt_secret: str = Field(default="", description="HS256 signing key; set in prod. Empty => ephemeral per-instance secret.")
    jwt_expire_minutes: int = Field(default=10080, ge=1, description="Session lifetime in minutes (default 7 days).")
    cookie_secure: bool = Field(default=True, description="Set the session cookie Secure flag. Set false for local http (curl) testing.")
```
(`Field` is already imported in config.py.)

- [ ] **Step 3: Document in .env.example**

In `.env.example`, append:
```
# Auth — HS256 session signing key. Generate one with: openssl rand -hex 32
# If unset, sessions are ephemeral (drop on restart / don't span instances).
JWT_SECRET=
JWT_EXPIRE_MINUTES=10080
# Set false only for local http testing with curl (default true is correct on https).
COOKIE_SECURE=true
```

- [ ] **Step 4: Deterministic JWT secret in tests**

In `tests/conftest.py`, with the other `os.environ[...]` overrides near the top (after `load_dotenv`), add:
```python
os.environ["JWT_SECRET"] = "test-secret-key-deterministic"
```

- [ ] **Step 5: Verify config loads**

Run: `PYTHONPATH=. venv/bin/python -c "from config import settings; print(settings.jwt_expire_minutes, repr(settings.jwt_secret)[:12])"`
Expected: prints `10080` and a secret repr.

- [ ] **Step 6: Commit**
```bash
git add requirements.txt config.py .env.example tests/conftest.py
git commit -m "chore(auth): add passlib/pyjwt deps + jwt config"
```

---

### Task 2: Password hashing (`auth/passwords.py`)

**Files:** Create `auth/__init__.py`, `auth/passwords.py`, `tests/unit/test_auth.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_auth.py`:
```python
"""Auth unit tests: passwords, tokens, users, routes."""
from __future__ import annotations

from auth.passwords import hash_password, verify_password


def test_hash_is_not_plaintext_and_verifies():
    h = hash_password("hunter2")
    assert h != "hunter2"
    assert verify_password("hunter2", h) is True


def test_verify_rejects_wrong_password():
    h = hash_password("hunter2")
    assert verify_password("nope", h) is False


def test_verify_handles_malformed_hash():
    assert verify_password("x", "not-a-real-hash") is False
```

- [ ] **Step 2: Run → fail**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: FAIL — `No module named 'auth'`.

- [ ] **Step 3: Implement**

Create `auth/__init__.py`:
```python
"""Authentication: password hashing, JWT sessions, gating."""
```
Create `auth/passwords.py`:
```python
"""Password hashing/verification (bcrypt via passlib)."""
from __future__ import annotations

from passlib.context import CryptContext

_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of a plaintext password."""
    return _ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if ``plain`` matches ``hashed``; False on any mismatch/error."""
    try:
        return _ctx.verify(plain, hashed)
    except Exception:  # noqa: BLE001 — malformed hash, etc. → not verified
        return False
```

- [ ] **Step 4: Run → pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add auth/__init__.py auth/passwords.py tests/unit/test_auth.py
git commit -m "feat(auth): bcrypt password hashing"
```

---

### Task 3: JWT tokens (`auth/tokens.py`)

**Files:** Create `auth/tokens.py`; append to `tests/unit/test_auth.py`

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_auth.py`)
```python
from auth.tokens import COOKIE_NAME, create_access_token, decode_token


def test_token_roundtrip_returns_user_id():
    tok = create_access_token("user-123")
    assert decode_token(tok) == "user-123"


def test_decode_rejects_tampered_token():
    tok = create_access_token("user-123")
    assert decode_token(tok + "x") is None


def test_decode_rejects_expired_token():
    tok = create_access_token("user-123", expires_minutes=-1)  # already expired
    assert decode_token(tok) is None


def test_cookie_name_is_stable():
    assert COOKIE_NAME == "nerve_session"
```

- [ ] **Step 2: Run → fail**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: FAIL — cannot import `auth.tokens`.

- [ ] **Step 3: Implement**

Create `auth/tokens.py`:
```python
"""JWT session tokens (HS256) and the session cookie name."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import jwt
import structlog

from config import settings

log = structlog.get_logger()

COOKIE_NAME = "nerve_session"
_ALGORITHM = "HS256"
#: Fallback secret when JWT_SECRET is unset — stable for this process only.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def _secret() -> str:
    """Return the signing secret (configured, or a per-process ephemeral one)."""
    if settings.jwt_secret:
        return settings.jwt_secret
    return _EPHEMERAL_SECRET


def create_access_token(user_id: str, *, expires_minutes: int | None = None) -> str:
    """Create a signed JWT carrying the user id as ``sub``.

    Args:
        user_id: The user this session belongs to.
        expires_minutes: Override the default lifetime (negative => already expired).

    Returns:
        The encoded JWT string.
    """
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes
    payload = {"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=minutes)}
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def decode_token(token: str | None) -> str | None:
    """Return the user id from a valid token, or None if invalid/expired/missing."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
```

- [ ] **Step 4: Run → pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**
```bash
git add auth/tokens.py tests/unit/test_auth.py
git commit -m "feat(auth): JWT session tokens"
```

---

### Task 4: User model + state layer + AuthError

**Files:** Modify `state/models.py`, `state/database.py`, `exceptions.py`; append to `tests/unit/test_auth.py`

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_auth.py`)
```python
import pytest

from exceptions import AuthError
from state import database as db


async def test_create_and_fetch_user(mock_db):
    user = await db.create_user("Alice@Example.com ", db_hash := "hashed")
    assert user.email == "alice@example.com"  # normalized
    assert user.password_hash == "hashed"
    by_email = await db.get_user_by_email("alice@example.com")
    assert by_email is not None and by_email.user_id == user.user_id
    by_id = await db.get_user(user.user_id)
    assert by_id is not None and by_id.email == "alice@example.com"


async def test_duplicate_email_rejected(mock_db):
    await db.create_user("bob@example.com", "h1")
    with pytest.raises(AuthError):
        await db.create_user("BOB@example.com", "h2")


async def test_get_user_missing_returns_none(mock_db):
    assert await db.get_user_by_email("nobody@example.com") is None
    assert await db.get_user("does-not-exist") is None
```

- [ ] **Step 2: Run → fail**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: FAIL — `cannot import name 'AuthError'` / `create_user`.

- [ ] **Step 3a: Add `AuthError`**

In `exceptions.py`, after the `NerveBaseError` subclasses (e.g. near `StateError`), add:
```python
class AuthError(NerveBaseError):
    """Authentication/registration failure (e.g. duplicate email, bad credentials)."""
```

- [ ] **Step 3b: Add the `User` model**

In `state/models.py`, after the `Action` model, add (confirm `_uuid` and `datetime` are imported — they are, used by `Action`):
```python
class User(_BaseDoc):
    """A registered account."""

    user_id: str = Field(default_factory=_uuid)
    email: str
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

- [ ] **Step 3c: Add the users collection + CRUD + index**

In `state/database.py`:
- Add the import: `from state.models import ... , User` (add `User` to the existing models import).
- Add the exception import: `from exceptions import AuthError` (add to the existing exceptions import line; if none, add it).
- Add a collection constant near the others: `COLLECTION_USERS = "users"`.
- Add the accessor (mirror `get_actions_collection`):
```python
def get_users_collection() -> AsyncIOMotorCollection:
    """Return the ``users`` collection."""
    return get_database()[COLLECTION_USERS]
```
- Add CRUD functions (mirror `create_action`/`_execute_read`):
```python
async def create_user(email: str, password_hash: str) -> User:
    """Create a user; raise AuthError if the (normalized) email already exists."""
    normalized = email.strip().lower()
    if await get_user_by_email(normalized) is not None:
        raise AuthError("email already registered", context={"email": normalized})
    user = User(email=normalized, password_hash=password_hash)
    ctx = {"user_id": user.user_id, "op": "create_user"}
    await _execute_write(
        lambda: get_users_collection().insert_one(user.model_dump()), error_ctx=ctx
    )
    log.info("user_created", user_id=user.user_id)
    return user


async def get_user_by_email(email: str) -> User | None:
    """Return the user with this (normalized) email, or None."""
    normalized = email.strip().lower()
    ctx = {"email": normalized, "op": "get_user_by_email"}
    doc = await _execute_read(lambda: get_users_collection().find_one({"email": normalized}), error_ctx=ctx)
    return _to_model(User, doc, error_ctx=ctx) if doc else None


async def get_user(user_id: str) -> User | None:
    """Return the user with this id, or None."""
    ctx = {"user_id": user_id, "op": "get_user"}
    doc = await _execute_read(lambda: get_users_collection().find_one({"user_id": user_id}), error_ctx=ctx)
    return _to_model(User, doc, error_ctx=ctx) if doc else None
```
- In `ensure_indexes()`, add (after the actions indexes):
```python
    users = get_users_collection()
    await users.create_index("user_id", unique=True, background=True)
    await users.create_index("email", unique=True, background=True)
```

> Note: `create_user` checks for an existing email before insert (works under mongomock, which doesn't enforce the unique index). The unique index is still added for the real DB as a second line of defense.

- [ ] **Step 4: Run → pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**
```bash
git add exceptions.py state/models.py state/database.py tests/unit/test_auth.py
git commit -m "feat(auth): User model + users collection + CRUD"
```

---

### Task 5: `current_user` dependency + gating middleware

**Files:** Create `auth/dependencies.py`, `auth/middleware.py`, `tests/unit/test_auth_middleware.py`

- [ ] **Step 1: Write failing middleware test**

Create `tests/unit/test_auth_middleware.py`:
```python
"""Middleware gating, exercised against a minimal ASGI app (no Mongo/lifespan)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from auth.middleware import AuthMiddleware
from auth.tokens import COOKIE_NAME, create_access_token


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/showcase")
    async def showcase():
        return {"ok": True}

    @app.get("/missions/x")
    async def gated_api():
        return {"ok": True}

    @app.get("/")
    async def gated_page():
        return {"ok": True}

    return app


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_public_paths_allowed_without_cookie():
    async with await _client(_app()) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/showcase")).status_code == 200


async def test_gated_api_returns_401_without_cookie():
    async with await _client(_app()) as c:
        r = await c.get("/missions/x")
        assert r.status_code == 401


async def test_gated_page_redirects_to_login_without_cookie():
    async with await _client(_app()) as c:
        r = await c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


async def test_valid_cookie_passes_gate():
    async with await _client(_app()) as c:
        c.cookies.set(COOKIE_NAME, create_access_token("u1"))
        assert (await c.get("/missions/x")).status_code == 200
```

- [ ] **Step 2: Run → fail**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth_middleware.py -q`
Expected: FAIL — cannot import `auth.middleware`.

- [ ] **Step 3a: Implement the dependency**

Create `auth/dependencies.py`:
```python
"""FastAPI dependency that resolves the current user from the session cookie."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from auth.tokens import COOKIE_NAME, decode_token
from state import database as db
from state.models import User


async def current_user(request: Request) -> User:
    """Return the authenticated user, or raise 401.

    Args:
        request: The incoming request (its cookies carry the session).

    Returns:
        The :class:`~state.models.User`.

    Raises:
        HTTPException: 401 if there is no valid session.
    """
    user_id = decode_token(request.cookies.get(COOKIE_NAME))
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = await db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user
```

- [ ] **Step 3b: Implement the middleware**

Create `auth/middleware.py`:
```python
"""HTTP middleware that gates every request outside a public allowlist.

A request is allowed through when its path starts with a public prefix, or when
it carries a valid session cookie. Otherwise: page (text/html) requests are
redirected to /login; everything else (APIs, WebSocket upgrades) gets 401.
"""
from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from auth.tokens import COOKIE_NAME, decode_token

log = structlog.get_logger()

# Public path prefixes — reachable without a session.
PUBLIC_PREFIXES = (
    "/auth", "/login", "/health", "/healthz", "/webhooks",
    "/showcase", "/live-classic", "/favicon.ico", "/docs", "/openapi.json", "/redoc",
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path == p for p in PUBLIC_PREFIXES) or any(
        path.startswith(p) for p in PUBLIC_PREFIXES
    )


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid session cookie for all non-public paths."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        if decode_token(request.cookies.get(COOKIE_NAME)) is not None:
            return await call_next(request)
        if _wants_html(request):
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
```

- [ ] **Step 4: Run → pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth_middleware.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**
```bash
git add auth/dependencies.py auth/middleware.py tests/unit/test_auth_middleware.py
git commit -m "feat(auth): current_user dependency + gating middleware"
```

---

### Task 6: Auth routes (`routes/auth.py`)

**Files:** Modify `routes/schemas.py`; create `routes/auth.py`; append to `tests/unit/test_auth.py`

- [ ] **Step 1: Write failing route tests** (append to `tests/unit/test_auth.py`)
```python
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import Response

from auth.tokens import COOKIE_NAME, decode_token
from routes import auth as auth_routes
from routes.schemas import LoginRequest, SignupRequest


def _req(cookie: str | None = None):
    cookies = {COOKIE_NAME: cookie} if cookie else {}
    return SimpleNamespace(cookies=cookies, headers={})


async def test_signup_creates_user_and_sets_cookie(mock_db):
    resp = Response()
    out = await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), resp)
    assert out.email == "a@b.com"
    cookie_header = resp.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie_header
    # the cookie encodes the new user's id
    token = cookie_header.split(COOKIE_NAME + "=")[1].split(";")[0]
    assert decode_token(token) == out.user_id


async def test_signup_duplicate_returns_409(mock_db):
    resp = Response()
    await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), resp)
    with pytest.raises(HTTPException) as exc:
        await auth_routes.signup(SignupRequest(email="A@b.com", password="password1"), Response())
    assert exc.value.status_code == 409


async def test_signup_short_password_returns_400(mock_db):
    with pytest.raises(HTTPException) as exc:
        await auth_routes.signup(SignupRequest(email="a@b.com", password="short"), Response())
    assert exc.value.status_code == 400


async def test_login_good_and_bad(mock_db):
    await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), Response())
    ok = await auth_routes.login(LoginRequest(email="a@b.com", password="password1"), Response())
    assert ok.email == "a@b.com"
    with pytest.raises(HTTPException) as exc:
        await auth_routes.login(LoginRequest(email="a@b.com", password="wrong"), Response())
    assert exc.value.status_code == 401


async def test_me_requires_session(mock_db):
    out = await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), Response())
    me = await auth_routes.me(await _current(out.user_id))
    assert me.user_id == out.user_id


async def _current(user_id):
    # helper: load the User the way current_user would
    return await db.get_user(user_id)
```

- [ ] **Step 2: Run → fail**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: FAIL — cannot import `routes.auth` / schemas.

- [ ] **Step 3a: Add schemas**

In `routes/schemas.py`, add:
```python
class SignupRequest(BaseModel):
    """Body for POST /auth/signup."""
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    """Body for POST /auth/login."""
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    """Public user view."""
    user_id: str
    email: str
```
(`BaseModel`, `Field` are already imported in schemas.py.)

- [ ] **Step 3b: Implement the routes**

Create `routes/auth.py`:
```python
"""Auth routes: signup, login, logout, me."""
from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status

from auth.dependencies import current_user
from auth.passwords import hash_password, verify_password
from auth.tokens import COOKIE_NAME, create_access_token
from config import settings
from exceptions import AuthError
from routes.schemas import LoginRequest, SignupRequest, UserResponse
from state import database as db
from state.models import User

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8


def _set_session_cookie(response: Response, user_id: str) -> None:
    """Attach the signed session cookie to a response."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_access_token(user_id),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )


@router.post("/signup", response_model=UserResponse, status_code=201)
async def signup(body: SignupRequest, response: Response) -> UserResponse:
    """Create an account and start a session.

    Raises:
        HTTPException: 400 (invalid email / short password) or 409 (duplicate).
    """
    if not _EMAIL_RE.match(body.email.strip()):
        raise HTTPException(status_code=400, detail="invalid email")
    if len(body.password) < _MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"password must be at least {_MIN_PASSWORD} characters")
    try:
        user = await db.create_user(body.email, hash_password(body.password))
    except AuthError as exc:
        raise HTTPException(status_code=409, detail="email already registered") from exc
    _set_session_cookie(response, user.user_id)
    log.info("signup", user_id=user.user_id)
    return UserResponse(user_id=user.user_id, email=user.email)


@router.post("/login", response_model=UserResponse)
async def login(body: LoginRequest, response: Response) -> UserResponse:
    """Verify credentials and start a session.

    Raises:
        HTTPException: 401 on invalid credentials.
    """
    user = await db.get_user_by_email(body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    _set_session_cookie(response, user.user_id)
    log.info("login", user_id=user.user_id)
    return UserResponse(user_id=user.user_id, email=user.email)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)) -> UserResponse:
    """Return the current user (401 if not logged in)."""
    return UserResponse(user_id=user.user_id, email=user.email)
```

- [ ] **Step 4: Run → pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_auth.py -q`
Expected: PASS (all auth tests). Note `secure=True` cookies are still emitted in tests (the Set-Cookie header contains the value regardless of transport).

- [ ] **Step 5: Commit**
```bash
git add routes/schemas.py routes/auth.py tests/unit/test_auth.py
git commit -m "feat(auth): signup/login/logout/me routes"
```

---

### Task 7: Login page + app wiring

**Files:** Create `dashboard/templates/login.html`; modify `routes/dashboard.py`, `main.py`

- [ ] **Step 1: Create the login page**

Create `dashboard/templates/login.html` — a self-contained NERVE-themed page (fonts/colors match the dashboard):
```html
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>NERVE — Sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
<style>
  *{box-sizing:border-box} body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(1200px 600px at 50% -10%,rgba(63,224,197,.06),transparent),#06090b;
    font-family:'Chakra Petch',sans-serif;color:#eafdf8}
  .card{width:min(380px,92vw);background:linear-gradient(180deg,#0e1416,#0a0f11);border:1px solid rgba(63,224,197,.22);
    border-radius:16px;padding:30px;box-shadow:0 24px 80px rgba(0,0,0,.6)}
  .mark{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .mark .name{font-weight:700;letter-spacing:2px} .mark .name b{color:#3fe0c5}
  h1{font-size:18px;margin:0 0 4px} .sub{color:#5d6e6c;font-size:13px;margin-bottom:18px;font-family:'IBM Plex Mono',monospace}
  label{display:block;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:1px;color:#5d6e6c;margin:12px 0 5px;text-transform:uppercase}
  input{width:100%;height:42px;padding:0 12px;background:#0a0f11;border:1px solid rgba(120,200,190,.2);border-radius:8px;color:#eafdf8;font-family:'IBM Plex Mono',monospace;font-size:14px}
  input:focus{outline:none;border-color:#3fe0c5}
  button{width:100%;height:44px;margin-top:18px;border:none;border-radius:8px;background:#3fe0c5;color:#04211c;font-weight:600;font-size:14px;cursor:pointer}
  button:hover{box-shadow:0 0 22px -4px #3fe0c5}
  .toggle{margin-top:16px;text-align:center;font-size:13px;color:#5d6e6c}
  .toggle a{color:#3fe0c5;cursor:pointer;text-decoration:none}
  .err{color:#ff6a72;font-size:12px;margin-top:10px;min-height:16px;font-family:'IBM Plex Mono',monospace}
</style></head>
<body>
  <div class="card">
    <div class="mark">
      <svg width="26" height="26" viewBox="0 0 28 28" fill="none"><path d="M3 19 C3 19 7 7 11 7 C15 7 13 21 17 21 C21 21 25 9 25 9" stroke="#3fe0c5" stroke-width="2" stroke-linecap="round"/><circle cx="3" cy="19" r="2.4" fill="#9d8bff"/><circle cx="25" cy="9" r="2.4" fill="#3fe0c5"/></svg>
      <span class="name">N<b>E</b>RVE</span>
    </div>
    <h1 id="title">Sign in</h1>
    <div class="sub" id="subtitle">Access the mission control plane</div>
    <form id="form" onsubmit="return submitForm(event)">
      <label>Email</label><input id="email" type="email" autocomplete="email" required />
      <label>Password</label><input id="password" type="password" autocomplete="current-password" required />
      <button id="submit" type="submit">Sign in</button>
      <div class="err" id="err"></div>
    </form>
    <div class="toggle" id="toggle">No account? <a onclick="setMode('signup')">Create one</a></div>
  </div>
<script>
  let mode = 'login';
  function setMode(m){ mode = m;
    document.getElementById('title').textContent = m==='login' ? 'Sign in' : 'Create account';
    document.getElementById('submit').textContent = m==='login' ? 'Sign in' : 'Sign up';
    document.getElementById('toggle').innerHTML = m==='login'
      ? "No account? <a onclick=\"setMode('signup')\">Create one</a>"
      : "Have an account? <a onclick=\"setMode('login')\">Sign in</a>";
    document.getElementById('err').textContent = '';
  }
  async function submitForm(e){ e.preventDefault();
    const email = document.getElementById('email').value.trim();
    const password = document.getElementById('password').value;
    const err = document.getElementById('err'); err.textContent = '';
    try {
      const res = await fetch('/auth/' + mode, { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ email, password }) });
      if (res.ok) { location.href = '/'; return false; }
      const d = await res.json().catch(()=>({}));
      err.textContent = d.detail || 'Something went wrong';
    } catch { err.textContent = 'Network error'; }
    return false;
  }
</script>
</body></html>
```

- [ ] **Step 2: Serve `/login` (public)**

In `routes/dashboard.py`, add a constant and route (mirror the existing routes):
```python
_LOGIN_HTML = _TEMPLATES / "login.html"


@router.get("/login", include_in_schema=False)
async def dashboard_login() -> FileResponse:
    """Serve the login / signup page (public)."""
    return FileResponse(_LOGIN_HTML)
```

- [ ] **Step 3: Wire the router + middleware in `main.py`**

In `main.py`:
- Add imports near the other route imports:
```python
from routes import actions, auth, dashboard, demo, failure, missions, webhooks
from auth.middleware import AuthMiddleware
```
- Register the middleware right after `app = FastAPI(...)` (line ~125):
```python
app.add_middleware(AuthMiddleware)
```
- Register the auth router with the others:
```python
app.include_router(auth.router)
```

- [ ] **Step 4: Smoke-test the app boots + gating works (manual)**

Run the offline harness: `venv/bin/python run_local_demo.py --port 8080` (background), then:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/login      # 200 (public)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/health     # 200 (public)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/showcase   # 200 (public)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/missions   # 401 (gated)
curl -s -o /dev/null -w "%{http_code}\n" -H "accept: text/html" http://localhost:8080/   # 302 → /login
```
Expected: 200, 200, 200, 401, 302.

- [ ] **Step 5: Full suite (no regressions)**

Run: `PYTHONPATH=. venv/bin/python -m pytest -q`
Expected: all pass (existing 184 + new auth tests). Existing route tests call handlers directly so the middleware doesn't affect them.

- [ ] **Step 6: Commit**
```bash
git add dashboard/templates/login.html routes/dashboard.py main.py
git commit -m "feat(auth): login page + register router/middleware"
```

---

### Task 8: Frontend — redirect to /login on 401

**Files:** Modify `dashboard/showcase_src/live-data.jsx`; rebuild templates

- [ ] **Step 1: Add a 401 guard to the API layer**

In `dashboard/showcase_src/live-data.jsx`, inside the `Api` object, add a helper and use it on the JSON fetches. Add near the top of `Api`:
```javascript
  _check(res) { if (res.status === 401) { location.href = '/login'; throw new Error('unauthorized'); } return res; },
```
Then in `getState`, change the fetch handling to call `_check`:
```javascript
  async getState(id) { const r = this._check(await fetch(`/missions/${id}`)); if (!r.ok) throw new Error('state ' + r.status); return r.json(); },
```
And in `listMissions`, after the fetch add the 401 guard:
```javascript
  async listMissions() { try { const r = await fetch('/missions'); if (r.status === 401) { location.href = '/login'; return []; } if (!r.ok) return []; const d = await r.json(); return (d && d.missions) || []; } catch (e) { return []; } },
```
(The WebSocket itself will fail to connect when unauthenticated; the poll/getState path catches the 401 and redirects, which is sufficient.)

- [ ] **Step 2: Rebuild the served templates**

Run: `venv/bin/python dashboard/showcase_src/build.py`
Expected: writes `index.html` and `live.html`.

- [ ] **Step 3: Commit**
```bash
git add dashboard/showcase_src/live-data.jsx dashboard/templates/index.html dashboard/templates/live.html
git commit -m "feat(auth): dashboard redirects to /login on 401"
```

---

### Task 9: End-to-end verification

**Files:** none (manual)

- [ ] **Step 1: Boot with non-Secure cookies (so curl over http works)**

`COOKIE_SECURE=false venv/bin/python run_local_demo.py --port 8080` (background). (In a browser you can leave it default — localhost is a secure context. `curl` won't send a Secure cookie over http, hence this override for the curl test below.)

- [ ] **Step 2: Sign up via API + confirm session + gated access**
```bash
# signup (saves the session cookie to a jar)
curl -s -c /tmp/jar.txt -X POST http://localhost:8080/auth/signup \
  -H "Content-Type: application/json" -d '{"email":"demo@nerve.io","password":"password1"}' -w "\n%{http_code}\n"
# /auth/me with the cookie → 200 + the user
curl -s -b /tmp/jar.txt http://localhost:8080/auth/me
# a gated API with the cookie → not 401 (200/empty list)
curl -s -b /tmp/jar.txt -o /dev/null -w "missions(authed)=%{http_code}\n" http://localhost:8080/missions
# without the cookie → 401
curl -s -o /dev/null -w "missions(anon)=%{http_code}\n" http://localhost:8080/missions
# logout clears the cookie
curl -s -b /tmp/jar.txt -c /tmp/jar.txt -X POST http://localhost:8080/auth/logout -w "logout=%{http_code}\n"
```
Expected: signup `201`; `/auth/me` returns the user; `missions(authed)=200`; `missions(anon)=401`; `logout=204`.

- [ ] **Step 3: Browser check (optional)**

Open `http://localhost:8080/` → redirected to `/login`. Sign up/in → lands on the live dashboard. `http://localhost:8080/showcase` works without login.

- [ ] **Step 4: Final commit (if any tidy-ups)**
```bash
git add -A && git commit -m "chore(auth): SP1 end-to-end verified"
```

---

## Self-Review

**Spec coverage:**
- User model + users collection + unique email → Task 4 ✅
- Password hashing (bcrypt) → Task 2 ✅
- JWT issuance/verification → Task 3 ✅
- Auth routes (signup/login/logout/me) → Task 6 ✅
- JWT httpOnly cookie session → Task 6 (`_set_session_cookie`) ✅
- Route gating via middleware + public allowlist → Task 5 + Task 7 ✅
- Login/signup page → Task 7 ✅
- Frontend 401 → /login → Task 8 ✅
- Config (`jwt_secret`, `jwt_expire_minutes`) → Task 1 ✅
- requirements (passlib, pyjwt) → Task 1 ✅
- Public vs gated split (`/showcase`,`/webhooks`,`/health` public; `/`,`/live`,APIs,`/ws` gated) → Task 5 `PUBLIC_PREFIXES` ✅
- Error handling (409/401/400/expired→redirect/401) → Tasks 5,6 ✅
- Testing (passwords/tokens/users/routes/gating) → Tasks 2–6 ✅
- Note: spec said existing route tests get an `auth_client`; in reality they call handlers directly and bypass middleware, so no change is needed — recorded in Task 7 Step 5. (Spec §7 over-stated this; the plan resolves it.)

**Placeholder scan:** none — every code step has full code; commands have expected output.

**Type consistency:** `COOKIE_NAME` ("nerve_session"), `create_access_token(user_id, *, expires_minutes=None)`, `decode_token(token)->str|None`, `User{user_id,email,password_hash,created_at}`, `db.create_user/get_user_by_email/get_user`, `AuthError`, `AuthMiddleware`, `current_user`, `SignupRequest/LoginRequest/UserResponse` — all used consistently across tasks.
