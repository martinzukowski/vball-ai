"""TokenVerifier against a mock JWKS. No database needed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from authkit import ISSUER, JWKS_URL, MockJwks, SigningKey, claims, token
from vball_api.auth.jwt import AuthError, AuthUnavailableError, JwksCache, TokenVerifier


def make_verifier(jwks: MockJwks) -> TokenVerifier:
    return TokenVerifier(JwksCache(JWKS_URL, jwks.client(), ttl_s=600), ISSUER, "authenticated")


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.ec()


async def test_valid_es256_token(key: SigningKey) -> None:
    sub = uuid.uuid4()
    c = await make_verifier(MockJwks([key])).verify(token(key, sub=str(sub)))
    assert c.user_id == sub
    assert c.email == "player@example.com"


async def test_valid_rs256_token() -> None:
    key = SigningKey.rsa()
    c = await make_verifier(MockJwks([key])).verify(token(key))
    assert isinstance(c.user_id, uuid.UUID)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"exp": int(time.time()) - 3600}, "token expired"),
        ({"aud": "anon"}, "invalid token"),
        ({"iss": "https://evil.supabase.co/auth/v1"}, "invalid token"),
        ({"sub": None}, "invalid token"),
        ({"exp": None}, "invalid token"),
        ({"sub": "not-a-uuid"}, "invalid subject"),
        ({"role": "anon"}, "not for a signed-in user"),
        ({"role": None}, "not for a signed-in user"),
    ],
)
async def test_rejected_claims(key: SigningKey, over: dict[str, object], message: str) -> None:
    with pytest.raises(AuthError, match=message):
        await make_verifier(MockJwks([key])).verify(token(key, **over))


async def test_forged_signature_rejected(key: SigningKey) -> None:
    attacker = SigningKey.ec(kid=key.kid)  # same kid, different private key
    with pytest.raises(AuthError, match="invalid token"):
        await make_verifier(MockJwks([key])).verify(token(attacker))


async def test_alg_none_rejected(key: SigningKey) -> None:
    unsigned = jwt.encode(claims(), None, algorithm="none", headers={"kid": key.kid})
    with pytest.raises(AuthError, match="algorithm not allowed"):
        await make_verifier(MockJwks([key])).verify(unsigned)


async def test_hs256_with_public_key_as_secret_rejected() -> None:
    # Classic algorithm-confusion attack: HMAC-sign with the server's *public* key.
    key = SigningKey.rsa()
    pem = key.private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # PyJWT refuses to HMAC-sign with a PEM key, so build the token by hand like an attacker.
    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": key.kid}).encode())
    body = b64(json.dumps(claims()).encode())
    sig = b64(hmac.new(pem, header + b"." + body, hashlib.sha256).digest())
    forged = (header + b"." + body + b"." + sig).decode()
    with pytest.raises(AuthError, match="algorithm not allowed"):
        await make_verifier(MockJwks([key])).verify(forged)


async def test_header_alg_must_match_key(key: SigningKey) -> None:
    rsa_key = SigningKey.rsa(kid=key.kid)
    # Token claims RS256 but the published key with that kid is EC.
    with pytest.raises(AuthError):
        await make_verifier(MockJwks([key])).verify(token(rsa_key))


@pytest.mark.parametrize("bad", ["", "abc", "a.b.c", "Bearer xyz"])
async def test_malformed_tokens(key: SigningKey, bad: str) -> None:
    with pytest.raises(AuthError):
        await make_verifier(MockJwks([key])).verify(bad)


async def test_missing_kid_rejected(key: SigningKey) -> None:
    t = jwt.encode(claims(), key.private, algorithm="ES256")
    with pytest.raises(AuthError, match="no key id"):
        await make_verifier(MockJwks([key])).verify(t)


async def test_key_rotation_refetches_once(key: SigningKey) -> None:
    jwks = MockJwks([key])
    verifier = make_verifier(jwks)
    await verifier.verify(token(key))
    assert jwks.fetches == 1

    rotated = SigningKey.ec(kid="key-ec-2")
    jwks.keys = [key, rotated]
    await verifier.verify(token(rotated))  # unknown kid -> one refetch
    assert jwks.fetches == 2

    # Unknown kids within the refetch interval do not hammer the JWKS endpoint.
    stranger = SigningKey.ec(kid="key-unknown")
    for _ in range(3):
        with pytest.raises(AuthError, match="unknown key"):
            await verifier.verify(token(stranger))
    assert jwks.fetches == 2


async def test_jwks_unreachable_is_unavailable_not_unauthorized(key: SigningKey) -> None:
    jwks = MockJwks([key], fail=True)
    with pytest.raises(AuthUnavailableError):
        await make_verifier(jwks).verify(token(key))


async def test_jwks_outage_after_first_fetch_keeps_known_keys(key: SigningKey) -> None:
    jwks = MockJwks([key])
    cache = JwksCache(JWKS_URL, jwks.client(), ttl_s=0)  # always considered stale
    verifier = TokenVerifier(cache, ISSUER, "authenticated")
    await verifier.verify(token(key))
    jwks.fail = True
    await verifier.verify(token(key))  # refresh fails, cached key still trusted


async def test_hs256_keys_in_jwks_are_ignored(key: SigningKey) -> None:
    jwks = MockJwks([key])
    original = jwks.handler

    def with_hmac(request):  # type: ignore[no-untyped-def]
        resp = original(request)
        body = resp.json()
        body["keys"].append(
            {
                "kty": "oct",
                "kid": "hmac",
                "alg": "HS256",
                "k": base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("="),
            }
        )
        return type(resp)(200, json=body)

    jwks.handler = with_hmac  # type: ignore[method-assign]
    verifier = make_verifier(jwks)
    hmac_token = jwt.encode(claims(), "s" * 32, algorithm="HS256", headers={"kid": "hmac"})
    with pytest.raises(AuthError):
        await verifier.verify(hmac_token)
