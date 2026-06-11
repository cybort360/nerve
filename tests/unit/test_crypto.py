"""Unit tests for the crypto helper (encrypt_secret / decrypt_secret)."""

from crypto import decrypt_secret, encrypt_secret


def test_encrypt_roundtrip():
    c = encrypt_secret("glpat-supersecret")
    assert c != "glpat-supersecret"
    assert decrypt_secret(c) == "glpat-supersecret"


def test_empty_string_is_passthrough():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_decrypt_garbage_returns_empty():
    assert decrypt_secret("not-a-valid-token") == ""
