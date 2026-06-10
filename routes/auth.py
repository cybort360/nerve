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

    Args:
        body: Signup request with email and password.
        response: FastAPI response object (used to set the session cookie).

    Returns:
        Public user view with user_id and email.

    Raises:
        HTTPException: 400 (invalid email / short password) or 409 (duplicate).
    """
    if not _EMAIL_RE.match(body.email.strip()):
        raise HTTPException(status_code=400, detail="invalid email")
    if len(body.password) < _MIN_PASSWORD:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {_MIN_PASSWORD} characters",
        )
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

    Args:
        body: Login request with email and password.
        response: FastAPI response object (used to set the session cookie).

    Returns:
        Public user view with user_id and email.

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
    """Clear the session cookie.

    Args:
        response: FastAPI response object used to delete the session cookie.
    """
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)) -> UserResponse:
    """Return the current user (401 if not logged in).

    Args:
        user: Resolved via the ``current_user`` dependency.

    Returns:
        Public user view with user_id and email.
    """
    return UserResponse(user_id=user.user_id, email=user.email)
