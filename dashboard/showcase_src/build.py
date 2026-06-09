#!/usr/bin/env python3
"""Re-inline the modular showcase source into ``dashboard/templates/index.html``.

The showcase dashboard (served at ``/``) is a self-contained, React+Babel-in-browser
page implemented from a Claude Design bundle. For editing convenience the original
modular source lives here (``*.jsx`` + ``styles.css`` + ``NERVE Dashboard.html``).
Edit those, then run this script to regenerate the single served file:

    python dashboard/showcase_src/build.py

The backend-wired dashboard is a separate file (``dashboard/templates/live.html``,
served at ``/live``) and is not touched by this script.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = SRC.parent / "templates" / "index.html"

# Script inline order is taken from the shell HTML itself, so it stays in sync.
SHELL = SRC / "NERVE Dashboard.html"


def build() -> None:
    """Assemble the self-contained showcase HTML from the modular source."""
    shell = SHELL.read_text()
    styles = (SRC / "styles.css").read_text()

    shell = shell.replace(
        '<link rel="stylesheet" href="styles.css" />',
        f"<style>\n{styles}\n</style>",
    )

    def inline(match: re.Match[str]) -> str:
        fname = match.group(1)
        content = (SRC / fname).read_text()
        return f'<script type="text/babel" data-source="{fname}">\n{content}\n</script>'

    shell = re.sub(r'<script type="text/babel" src="([^"]+)"></script>', inline, shell)

    shell = shell.replace(
        "<!DOCTYPE html>",
        "<!DOCTYPE html>\n<!-- NERVE Mission Dashboard — generated from dashboard/showcase_src "
        "by build.py. Edit the modular source there, not this file. /live serves the "
        "backend-wired dashboard. -->",
    )

    OUT.write_text(shell)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
