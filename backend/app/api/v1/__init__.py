"""Version 1 of the NEXUS HTTP API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import meta
from app.modules.proxy_manager.router import router as proxy_router

api_v1_router = APIRouter(prefix="/v1")
api_v1_router.include_router(meta.router)
api_v1_router.include_router(proxy_router)

__all__ = ["api_v1_router"]
