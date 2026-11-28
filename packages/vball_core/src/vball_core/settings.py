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
    # Invite-only: comma-separated emails allowed to use the API (case-insensitive). Empty =
    # anyone with a valid Supabase account (fine locally; set it when deployed).
    allowed_emails: str = ""
    jwks_cache_ttl_s: int = Field(default=600, ge=30, le=86400)

    # S3-compatible object storage (Cloudflare R2 in production, SeaweedFS locally).
    s3_endpoint_url: str | None = None
    # Endpoint used in presigned URLs handed to the browser. Differs from s3_endpoint_url only
    # locally, where the API reaches storage at http://s3:8333 but the browser at localhost.
    s3_public_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket: str = "vball"

    # Uploads. R2's free tier is 10 GB in total, so stored bytes are capped below that.
    max_upload_bytes: int = Field(default=20 * 1024**3, gt=0)
    storage_cap_bytes: int = Field(default=9 * 1024**3, gt=0)
    upload_url_ttl_s: int = Field(default=3600, ge=60, le=7 * 86400)
    upload_expiry_s: int = Field(default=7 * 86400, ge=3600)

    # Worker / ingest. Originals are deleted after a playback proxy exists unless
    # keep_originals is set, to stay inside the storage free tier.
    proxy_height: int = Field(default=720, ge=240, le=2160)
    keep_originals: bool = False
    max_video_duration_s: int = Field(default=4 * 3600, gt=0)
    worker_poll_interval_s: float = Field(default=3.0, gt=0)
    worker_scratch_dir: str | None = None
    # Deployed on Modal: the API calls the `wake` endpoint of infra/modal/vball_app.py with this
    # bearer token. Takes precedence over the GitHub dispatch below.
    worker_wake_url: str | None = None
    worker_wake_token: SecretStr | None = None
    # Deployed: the API starts the GitHub Actions worker ("owner/repo" + a fine-grained token
    # allowed to write repository contents, which repository_dispatch requires).
    worker_dispatch_repo: str | None = None
    worker_dispatch_token: SecretStr | None = None

    @property
    def storage_configured(self) -> bool:
        return self.s3_access_key_id is not None and self.s3_secret_access_key is not None

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
    def allowed_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.allowed_emails.split(",") if e.strip())

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
