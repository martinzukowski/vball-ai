"""Environment-based configuration shared by the API and workers.

Every value comes from environment variables prefixed with ``VBALL_`` (or a local ``.env``
file in development). Nothing secret has a default.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, HttpUrl, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VBALL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_json: bool = True

    # SQLAlchemy URL using the psycopg 3 driver, e.g.
    # postgresql+psycopg://user:password@host:5432/dbname
    database_url: SecretStr = Field(...)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_connect_timeout_s: int = Field(default=5, ge=1, le=60)

    # Comma-separated list of origins allowed to call the API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Supabase Auth. The API verifies access tokens locally against the project's public JWKS,
    # so the project must use asymmetric JWT signing keys (ES256/RS256), not the legacy HS256
    # shared secret. Optional only in local/test environments; required when deployed.
    supabase_url: HttpUrl | None = None
    jwt_audience: str = "authenticated"
    jwks_cache_ttl_s: int = Field(default=600, ge=30, le=86400)

    @model_validator(mode="after")
    def _auth_required_when_deployed(self) -> Settings:
        if self.supabase_url is None and self.env in (Environment.STAGING, Environment.PRODUCTION):
            raise ValueError("VBALL_SUPABASE_URL is required in staging and production")
        return self

    @property
    def supabase_auth_base(self) -> str | None:
        if self.supabase_url is None:
            return None
        return str(self.supabase_url).rstrip("/") + "/auth/v1"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def database_dsn(self) -> str:
        url = self.database_url.get_secret_value()
        # Validate shape early so a typo fails at startup, not at first query.
        PostgresDsn(url)
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment (pydantic mypy plugin knows this)
