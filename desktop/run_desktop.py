"""Entry point used for packaging.

PyInstaller needs a script at the project root so the ``app`` package is a sibling of the entry
point (``from app.main import main`` then resolves inside the bundle). Keeping this separate from
``app/main.py`` means the same code path serves both ``python -m app.main`` and the built
executable.
"""

from __future__ import annotations

import sys

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
