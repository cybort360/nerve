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


def test_long_password_does_not_crash():
    long = "a" * 200  # > 72 bytes
    h = hash_password(long)
    assert verify_password(long, h) is True


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


import pytest

from exceptions import AuthError
from state import database as db


async def test_create_and_fetch_user(mock_db):
    user = await db.create_user("Alice@Example.com ", "hashed")
    assert user.email == "alice@example.com"  # normalized (trimmed + lowercased)
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


# ---------------------------------------------------------------------------
# Route handler tests
# ---------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from auth.tokens import COOKIE_NAME, decode_token  # noqa: E402
from routes import auth as auth_routes  # noqa: E402
from routes.schemas import LoginRequest, SignupRequest  # noqa: E402


async def test_signup_creates_user_and_sets_cookie(mock_db):
    resp = Response()
    out = await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), resp)
    assert out.email == "a@b.com"
    cookie_header = resp.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie_header
    token = cookie_header.split(COOKIE_NAME + "=")[1].split(";")[0]
    assert decode_token(token) == out.user_id


async def test_signup_duplicate_returns_409(mock_db):
    await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), Response())
    with pytest.raises(HTTPException) as exc:
        await auth_routes.signup(SignupRequest(email="A@b.com", password="password1"), Response())
    assert exc.value.status_code == 409


async def test_signup_short_password_returns_400(mock_db):
    with pytest.raises(HTTPException) as exc:
        await auth_routes.signup(SignupRequest(email="a@b.com", password="short"), Response())
    assert exc.value.status_code == 400


async def test_signup_invalid_email_returns_400(mock_db):
    with pytest.raises(HTTPException) as exc:
        await auth_routes.signup(SignupRequest(email="not-an-email", password="password1"), Response())
    assert exc.value.status_code == 400


async def test_login_good_and_bad(mock_db):
    await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), Response())
    ok = await auth_routes.login(LoginRequest(email="a@b.com", password="password1"), Response())
    assert ok.email == "a@b.com"
    with pytest.raises(HTTPException) as exc:
        await auth_routes.login(LoginRequest(email="a@b.com", password="wrong"), Response())
    assert exc.value.status_code == 401


async def test_me_returns_current_user(mock_db):
    out = await auth_routes.signup(SignupRequest(email="a@b.com", password="password1"), Response())
    user = await db.get_user(out.user_id)
    me = await auth_routes.me(user)
    assert me.user_id == out.user_id and me.email == "a@b.com"


async def test_login_unknown_email_returns_401(mock_db):
    with pytest.raises(HTTPException) as exc:
        await auth_routes.login(LoginRequest(email="ghost@nowhere.io", password="password1"), Response())
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid credentials"
