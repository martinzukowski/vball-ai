"""FastAPI dependencies: database session, authenticated user, ownership checks."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vball_api.auth.jwt import AuthError, AuthUnavailableError, Claims, TokenVerifier
from vball_core.db.models import Profile

_bearer = HTTPBearer(auto_error=False, description="Supabase Auth access token")


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Claims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing bearer token")
    verifier: TokenVerifier | None = request.app.state.token_verifier
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication not configured")
    try:
        return await verifier.verify(credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc
    except AuthUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def get_current_user(
    request: Request, claims: Annotated[Claims, Depends(get_claims)], db: DbSession
) -> Claims:
    """Verified, invited user; ensures a profile row exists (created on first request).
    With an allowlist set, a valid account whose email isn't on it gets 403 and no profile."""
    allowed = request.app.state.settings.allowed_email_set
    if allowed and (claims.email or "").lower() not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account hasn't been invited to VBall.")
    await db.execute(
        insert(Profile).values(id=claims.user_id).on_conflict_do_nothing(index_elements=["id"])
    )
    await db.commit()
    return claims


CurrentUser = Annotated[Claims, Depends(get_current_user)]


def ensure_owner(owner_id: uuid.UUID | None, user: Claims) -> None:
    """Raise 404 (not 403) when a resource is missing or owned by someone else, so callers
    cannot probe which ids exist."""
    if owner_id is None or owner_id != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
