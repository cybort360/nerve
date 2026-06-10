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
