"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from anyio import to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vball_api.auth.jwt import JwksCache, TokenVerifier
from vball_api.routers import health, highlights, me, videos
from vball_core import __version__
from vball_core.db.session import create_engine, create_sessionmaker
from vball_core.logging import configure_logging, get_logger
from vball_core.settings import Environment, Settings, get_settings
from vball_core.storage import ObjectStorage

# Routes reachable without a bearer token. Every other route must require auth; a test walks
# the route table to enforce this.
PUBLIC_PATHS = frozenset(
    {"/healthz", "/readyz", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)


def create_app(
    settings: Settings | None = None, http_client: httpx.AsyncClient | None = None
) -> FastAPI:
    """Build the app. ``http_client`` lets tests inject a transport for the JWKS fetch."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("vball_api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        client = http_client or httpx.AsyncClient()
        auth_base = settings.supabase_auth_base
        app.state.token_verifier = (
            TokenVerifier(
                JwksCache(f"{auth_base}/.well-known/jwks.json", client, settings.jwks_cache_ttl_s),
                issuer=auth_base,
                audience=settings.jwt_audience,
            )
            if auth_base
            else None
        )
        app.state.storage = ObjectStorage(settings) if settings.storage_configured else None
        if app.state.storage is None:
            log.warning("storage_not_configured", hint="set VBALL_S3_* variables")
        elif settings.env in (Environment.LOCAL, Environment.TEST):
            try:  # convenience for the local S3 stand-in; production buckets are pre-created
                await to_thread.run_sync(app.state.storage.ensure_bucket)
            except Exception as exc:
                log.warning("storage_bucket_check_failed", error=type(exc).__name__)
        if auth_base is None:
            log.warning("auth_not_configured", hint="set VBALL_SUPABASE_URL")
        log.info("api_started", env=settings.env.value, version=__version__)
        try:
            yield
        finally:
            if http_client is None:
                await client.aclose()
            await engine.dispose()
            log.info("api_stopped")

    # Interactive docs only outside production (the schema isn't secret, but no need to serve it).
    deployed = settings.env in (Environment.STAGING, Environment.PRODUCTION)
    app = FastAPI(
        title="VBall API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if deployed else "/docs",
        redoc_url=None if deployed else "/redoc",
        openapi_url=None if deployed else "/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(videos.router)
    app.include_router(highlights.router)
    return app
