"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vball_core import __version__
from vball_core.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger("vball_api.health")


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class CheckResult(BaseModel):
    ok: bool
    error: str | None = None


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


@router.get("/healthz", response_model=Health, operation_id="getHealth")
async def healthz() -> Health:
    """Liveness: the process is up. Does not touch dependencies."""
    return Health(status="ok", version=__version__)


@router.get(
    "/readyz",
    response_model=Readiness,
    operation_id="getReadiness",
    responses={503: {"model": Readiness}},
)
async def readyz(request: Request, response: Response) -> Readiness:
    """Readiness: the database and object storage are reachable."""
    engine: AsyncEngine = request.app.state.engine
    checks: dict[str, CheckResult] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = CheckResult(ok=True)
    except Exception as exc:
        log.warning("readiness_db_failed", error=type(exc).__name__)
        checks["database"] = CheckResult(ok=False, error=type(exc).__name__)

    storage = request.app.state.storage
    if storage is None:
        checks["storage"] = CheckResult(ok=False, error="not configured")
    else:
        try:
            await to_thread.run_sync(storage.check)
            checks["storage"] = CheckResult(ok=True)
        except Exception as exc:
            log.warning("readiness_storage_failed", error=type(exc).__name__)
            checks["storage"] = CheckResult(ok=False, error=type(exc).__name__)

    ready = all(c.ok for c in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "not_ready", checks=checks)
