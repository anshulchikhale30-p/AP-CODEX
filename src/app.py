"""Vercel entrypoint for the AP-CODEX FastAPI backend.

Vercel discovers a FastAPI instance named ``app`` at a supported entrypoint
(the ``src/app.py`` filename qualifies). The application factory and routes
actually live in :mod:`api.app`, whose module imports assume ``src/`` is on
``sys.path`` — so we add it here before importing the real app.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENTRYPOINT_DIR = Path(__file__).resolve().parent
if str(_ENTRYPOINT_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTRYPOINT_DIR))

from api.app import app  # noqa: E402