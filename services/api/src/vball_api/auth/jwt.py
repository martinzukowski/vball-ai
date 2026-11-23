"""Verification of Supabase Auth access tokens against the project's public JWKS.

Only asymmetric algorithms are accepted. HS256 (shared secret) and ``none`` are rejected,
which also rules out algorithm-confusion attacks where a public key is used as an HMAC secret.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from vball_core.logging import get_logger

ALLOWED_ALGORITHMS = frozenset({"ES256", "RS256", "EdDSA"})
UNKNOWN_KID_REFETCH_INTERVAL_S = 30.0

log = get_logger("vball_api.auth")


class AuthError(Exception):
    """The token is missing, malformed, expired or not trusted. Message is safe to return."""


class AuthUnavailableError(Exception):
    """Auth cannot be checked right now (not configured, or JWKS unreachable)."""


@dataclass(frozen=True, slots=True)
class Claims:
    user_id: uuid.UUID
    email: str | None
    expires_at: int


class JwksCache:
    """Caches the JWKS; refetches after ``ttl_s`` or (rate-limited) when a kid is unknown."""

    def __init__(self, url: str, client: httpx.AsyncClient, ttl_s: float) -> None:
        self._url = url
        self._client = client
        self._ttl_s = ttl_s
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._last_unknown_refetch = 0.0
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> PyJWK:
        now = time.monotonic()
        if not self._keys or now - self._fetched_at > self._ttl_s:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None and time.monotonic() - self._last_unknown_refetch > (
            UNKNOWN_KID_REFETCH_INTERVAL_S
        ):
            # Keys may have been rotated since the last fetch.
            self._last_unknown_refetch = time.monotonic()
            await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise AuthError("token signed with an unknown key")
        return key

    async def _refresh(self) -> None:
        async with self._lock:
            try:
                resp = await self._client.get(self._url, timeout=5.0)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.error("jwks_fetch_failed", error=type(exc).__name__)
                if self._keys:  # keep serving with the keys we already trust
                    return
                raise AuthUnavailableError("could not load signing keys") from exc
            keys: dict[str, PyJWK] = {}
            for jwk in body.get("keys", []):
                kid, alg = jwk.get("kid"), jwk.get("alg")
                if not kid or alg not in ALLOWED_ALGORITHMS:
                    continue
                try:
                    keys[kid] = PyJWK(jwk)
                except jwt.PyJWKError:
                    log.warning("jwks_key_skipped", kid=kid)
            if not keys:
                log.error("jwks_has_no_usable_keys", url=self._url)
            self._keys = keys
            self._fetched_at = time.monotonic()


class TokenVerifier:
    def __init__(self, jwks: JwksCache, issuer: str, audience: str) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience

    async def verify(self, token: str) -> Claims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise AuthError("malformed token") from exc
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALLOWED_ALGORITHMS:
            raise AuthError("token algorithm not allowed")
        if not isinstance(kid, str):
            raise AuthError("token has no key id")

        key = await self._jwks.get(kid)
        if key.algorithm_name != alg:
            raise AuthError("token algorithm does not match its key")
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("invalid token") from exc

        # Supabase also issues tokens for the "anon" role; only signed-in users are accepted.
        if payload.get("role") != "authenticated":
            raise AuthError("token is not for a signed-in user")
        try:
            user_id = uuid.UUID(str(payload["sub"]))
        except ValueError as exc:
            raise AuthError("invalid subject") from exc
        email = payload.get("email")
        return Claims(
            user_id=user_id,
            email=email if isinstance(email, str) and email else None,
            expires_at=int(payload["exp"]),
        )
