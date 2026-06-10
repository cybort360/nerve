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
