"""Password hashing/verification (bcrypt)."""
from __future__ import annotations

import bcrypt

#: bcrypt only uses the first 72 bytes of a password; truncate to stay within
#: bcrypt 5.x's hard limit (it raises on longer inputs).
_MAX_BCRYPT_BYTES = 72


def _encode(plain: str) -> bytes:
    return plain.encode("utf-8")[:_MAX_BCRYPT_BYTES]


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of a plaintext password (first 72 bytes used)."""
    return bcrypt.hashpw(_encode(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if ``plain`` matches ``hashed``; False on any mismatch/error."""
    try:
        return bcrypt.checkpw(_encode(plain), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed hash, etc. → not verified
        return False
