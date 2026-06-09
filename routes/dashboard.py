"""Dashboard route: serves the single-page mission dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

_TEMPLATES = Path(__file__).resolve().parent.parent / "dashboard" / "templates"
_INDEX_HTML = _TEMPLATES / "index.html"
_LIVE_HTML = _TEMPLATES / "live.html"


@router.get("/", include_in_schema=False)
async def dashboard_index() -> FileResponse:
    """Serve the showcase mission dashboard (self-contained, scripted demo)."""
    return FileResponse(_INDEX_HTML)


@router.get("/live", include_in_schema=False)
async def dashboard_live() -> FileResponse:
    """Serve the backend-wired dashboard (polls/streams the live mission API)."""
    return FileResponse(_LIVE_HTML)
