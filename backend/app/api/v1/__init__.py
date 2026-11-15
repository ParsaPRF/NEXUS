"""Version 1 of the NEXUS HTTP API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import meta
from app.modules.email_manager.router import router as email_router
from app.modules.proxy_manager.router import router as proxy_router
from app.modules.registration_lab.router import router as registration_router
from app.modules.task_manager.router import router as job_router

api_v1_router = APIRouter(prefix="/v1")
api_v1_router.include_router(meta.router)
api_v1_router.include_router(proxy_router)
api_v1_router.include_router(email_router)
api_v1_router.include_router(registration_router)
api_v1_router.include_router(job_router)

__all__ = ["api_v1_router"]
