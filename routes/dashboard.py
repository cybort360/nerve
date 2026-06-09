"""Dashboard route: serves the single-page mission dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

_INDEX_HTML = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "index.html"


@router.get("/", include_in_schema=False)
async def dashboard_index() -> FileResponse:
    """Serve the dashboard HTML (polls the mission API client-side)."""
    return FileResponse(_INDEX_HTML)
