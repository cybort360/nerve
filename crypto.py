"""Symmetric encryption for per-user integration secrets at rest (Fernet)."""
from __future__ import annotations

import base64
import hashlib

import structlog
from cryptography.fernet import Fernet, InvalidToken

from config import settings

log = structlog.get_logger()


def _fernet() -> Fernet:
    """Build the Fernet from SETTINGS_ENCRYPTION_KEY, or a derived dev key if unset."""
    key = settings.settings_encryption_key
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    # Dev fallback: stable per-process key derived from the (possibly empty) value.
    # Production MUST set SETTINGS_ENCRYPTION_KEY so secrets survive restarts.
    derived = base64.urlsafe_b64encode(hashlib.sha256(b"nerve-dev-settings-key").digest())
    return Fernet(derived)


def encrypt_secret(plain: str) -> str:
    """Encrypt a secret string; empty string passes through unchanged.

    Args:
        plain: Plaintext secret to encrypt.

    Returns:
        Fernet-encrypted token as a UTF-8 string, or ``""`` if ``plain`` is empty.
    """
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Decrypt; return '' on empty input or any decryption failure.

    Args:
        token: Fernet-encrypted token string.

    Returns:
        Decrypted plaintext, or ``""`` on empty input or any error.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        log.warning("settings_secret_decrypt_failed")
        return ""
