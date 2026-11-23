"""Test helpers: real key pairs, a mock JWKS endpoint and a token factory."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

SUPABASE_URL = "https://testproj.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"


@dataclass
class SigningKey:
    kid: str
    alg: str
    private: Any

    @classmethod
    def ec(cls, kid: str = "key-ec-1") -> SigningKey:
        return cls(kid, "ES256", ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def rsa(cls, kid: str = "key-rsa-1") -> SigningKey:
        return cls(kid, "RS256", rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def public_jwk(self) -> dict[str, Any]:
        algo = ECAlgorithm if self.alg == "ES256" else RSAAlgorithm
        jwk: dict[str, Any] = json.loads(algo.to_jwk(self.private.public_key()))
        jwk.update({"kid": self.kid, "alg": self.alg, "use": "sig"})
        return jwk


def claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "email": "player@example.com",
        "role": "authenticated",
        "aud": "authenticated",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def token(key: SigningKey, **over: Any) -> str:
    return jwt.encode(claims(**over), key.private, algorithm=key.alg, headers={"kid": key.kid})


@dataclass
class MockJwks:
    """Serves whichever keys are currently published; counts fetches; can simulate outages."""

    keys: list[SigningKey]
    fail: bool = False
    fetches: int = 0
    calls: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        self.fetches += 1
        if self.fail:
            return httpx.Response(503)
        if str(request.url) != JWKS_URL:
            return httpx.Response(404)
        return httpx.Response(200, json={"keys": [k.public_jwk() for k in self.keys]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
