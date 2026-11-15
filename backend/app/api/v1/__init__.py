"""Version 1 of the NEXUS HTTP API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import meta

api_v1_router = APIRouter(prefix="/v1")
api_v1_router.include_router(meta.router)

__all__ = ["api_v1_router"]
