from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from vball_api.main import create_app
from vball_core import __version__
from vball_core.settings import Environment, Settings


def make_settings(url: str) -> Settings:
    return Settings(
        env=Environment.TEST, database_url=SecretStr(url), log_json=False, db_connect_timeout_s=2
    )


async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def unreachable_db_client() -> AsyncIterator[httpx.AsyncClient]:
    # Port 1 on localhost: connection refused immediately.
    app = create_app(make_settings("postgresql+psycopg://u:p@127.0.0.1:1/none"))
    async for c in client_for(app):
        yield c


async def test_healthz_does_not_need_database(unreachable_db_client: httpx.AsyncClient) -> None:
    r = await unreachable_db_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


async def test_readyz_reports_unreachable_database(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    r = await unreachable_db_client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False


@pytest.mark.db
async def test_readyz_ok_with_database(migrated_url: str) -> None:
    app = create_app(make_settings(migrated_url))
    async for client in client_for(app):
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready", "checks": {"database": {"ok": True, "error": None}}}


async def test_cors_allows_only_configured_origin(unreachable_db_client: httpx.AsyncClient) -> None:
    allowed = await unreachable_db_client.get(
        "/healthz", headers={"Origin": "http://localhost:3000"}
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    denied = await unreachable_db_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers
