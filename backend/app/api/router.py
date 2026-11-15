"""API router assembly.

Everything the HTTP layer exposes is mounted here. ``app.main`` imports exactly one object from
this module, so adding a new module's routes is a one-line change in one place - and there is no
way for a router to become reachable "by accident" without appearing in this file.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.api.v1 import api_v1_router

#: Unversioned operational endpoints (probes). Not part of the public API contract.
operational_router = APIRouter()
operational_router.include_router(health.router)

#: The public API. A future breaking change ships as ``/api/v2`` alongside this one, which is
#: why the version lives in the prefix rather than in each path.
api_router = APIRouter(prefix="/api")
api_router.include_router(api_v1_router)

__all__ = ["api_router", "operational_router"]
