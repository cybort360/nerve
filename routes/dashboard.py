"""Dashboard route: serves the single-page mission dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

_TEMPLATES = Path(__file__).resolve().parent.parent / "dashboard" / "templates"
_INDEX_HTML = _TEMPLATES / "index.html"
_LIVE_HTML = _TEMPLATES / "live.html"
_LIVE_CLASSIC_HTML = _TEMPLATES / "live-classic.html"
_LOGIN_HTML = _TEMPLATES / "login.html"
_ACCOUNT_HTML = _TEMPLATES / "account.html"


@router.get("/login", include_in_schema=False)
async def dashboard_login() -> FileResponse:
    """Serve the login / signup page (public)."""
    return FileResponse(_LOGIN_HTML)


@router.get("/", include_in_schema=False)
async def dashboard_index() -> FileResponse:
    """Serve the LIVE dashboard (showcase UI driven by the real mission API).

    This is the front door — visitors run real missions here. The scripted,
    always-on demo is at ``/showcase``.
    """
    return FileResponse(_LIVE_HTML)


@router.get("/live", include_in_schema=False)
async def dashboard_live() -> FileResponse:
    """Alias for the live dashboard (same as ``/``)."""
    return FileResponse(_LIVE_HTML)


@router.get("/showcase", include_in_schema=False)
async def dashboard_showcase() -> FileResponse:
    """Serve the self-contained scripted demo (no backend needed — always plays)."""
    return FileResponse(_INDEX_HTML)


@router.get("/live-classic", include_in_schema=False)
async def dashboard_live_classic() -> FileResponse:
    """Serve the original vanilla backend-wired dashboard (fallback)."""
    return FileResponse(_LIVE_CLASSIC_HTML)


@router.get("/account", include_in_schema=False)
async def dashboard_account() -> FileResponse:
    """Serve the per-user settings page (login required)."""
    return FileResponse(_ACCOUNT_HTML)
