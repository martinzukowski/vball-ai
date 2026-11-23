"""Auth enforced through the HTTP layer, with a real database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import HTTPException
from pydantic import HttpUrl, SecretStr, ValidationError
from sqlalchemy import create_engine, text

from authkit import SUPABASE_URL, MockJwks, SigningKey, token
from vball_api.auth.jwt import Claims
from vball_api.deps import ensure_owner
from vball_api.main import PUBLIC_PATHS, create_app
from vball_core.settings import Environment, Settings


def settings_for(url: str, *, supabase: bool = True) -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url=SecretStr(url),
        log_json=False,
        supabase_url=HttpUrl(SUPABASE_URL) if supabase else None,
    )


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.ec()


@pytest.fixture
def jwks(key: SigningKey) -> MockJwks:
    return MockJwks([key])


async def _client(app) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def client(migrated_url: str, jwks: MockJwks) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings_for(migrated_url), http_client=jwks.client())
    async for c in _client(app):
        yield c


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.db
async def test_me_requires_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.db
async def test_me_creates_profile_once(
    client: httpx.AsyncClient, key: SigningKey, migrated_url: str
) -> None:
    sub = uuid.uuid4()
    for _ in range(2):
        r = await client.get("/me", headers=bearer(token(key, sub=str(sub))))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(sub)
        assert body["email"] == "player@example.com"
        assert body["quota_storage_bytes"] > 0
    engine = create_engine(migrated_url)
    try:
        with engine.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM profiles WHERE id = :id"), {"id": sub}
            ).scalar_one()
    finally:
        engine.dispose()
    assert n == 1


@pytest.mark.db
async def test_users_only_see_themselves(client: httpx.AsyncClient, key: SigningKey) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    ra = await client.get("/me", headers=bearer(token(key, sub=str(a))))
    rb = await client.get("/me", headers=bearer(token(key, sub=str(b))))
    assert ra.json()["id"] == str(a)
    assert rb.json()["id"] == str(b)


@pytest.mark.db
async def test_expired_and_forged_tokens_get_401(
    client: httpx.AsyncClient, key: SigningKey
) -> None:
    expired = token(key, exp=1, iat=0)
    forged = token(SigningKey.ec(kid=key.kid))
    for t in (expired, forged, "garbage"):
        r = await client.get("/me", headers=bearer(t))
        assert r.status_code == 401, t


@pytest.mark.db
async def test_jwks_outage_returns_503(migrated_url: str, key: SigningKey) -> None:
    app = create_app(settings_for(migrated_url), http_client=MockJwks([key], fail=True).client())
    async for c in _client(app):
        r = await c.get("/me", headers=bearer(token(key)))
        assert r.status_code == 503


@pytest.mark.db
async def test_auth_not_configured_returns_503(migrated_url: str, key: SigningKey) -> None:
    app = create_app(settings_for(migrated_url, supabase=False))
    async for c in _client(app):
        r = await c.get("/me", headers=bearer(token(key)))
        assert r.status_code == 503


@pytest.mark.db
async def test_every_non_public_route_requires_auth(client: httpx.AsyncClient) -> None:
    """Guards future routes: anything not explicitly public must reject anonymous calls."""
    # Enumerate from the OpenAPI schema: stable across FastAPI versions. Routes hidden from the
    # schema (include_in_schema=False) are not covered, so do not hide authenticated routes.
    app = create_app(settings_for("postgresql+psycopg://u:p@localhost:5432/x"))
    paths = app.openapi()["paths"]
    protected = {p: ops for p, ops in paths.items() if p not in PUBLIC_PATHS}
    assert protected, "expected at least one protected route"
    for path, ops in protected.items():
        literal = path.replace("{", "").replace("}", "")  # placeholders -> literal segments
        for method in ops:
            r = await client.request(method.upper(), literal)
            assert r.status_code == 401, f"{method.upper()} {path} returned {r.status_code}"


def test_ensure_owner() -> None:
    user = Claims(user_id=uuid.uuid4(), email=None, expires_at=0)
    ensure_owner(user.user_id, user)
    for other in (uuid.uuid4(), None):
        with pytest.raises(HTTPException) as exc:
            ensure_owner(other, user)
        assert exc.value.status_code == 404


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_deployed_environments_require_supabase(env: Environment) -> None:
    with pytest.raises(ValidationError, match="VBALL_SUPABASE_URL"):
        Settings(env=env, database_url=SecretStr("postgresql+psycopg://u:p@h:5432/d"))
