"""Password hashing/verification (bcrypt via passlib)."""
from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of a plaintext password."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if ``plain`` matches ``hashed``; False on any mismatch/error."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:  # noqa: BLE001 — malformed hash, etc. → not verified
        return False
