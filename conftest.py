"""Shared pytest fixtures.

Database tests need ``VBALL_TEST_DATABASE_URL`` pointing at a PostgreSQL server where the user
may create databases, e.g. the local compose stack:

    VBALL_TEST_DATABASE_URL=postgresql+psycopg://vball:vball-local-only@localhost:5432/postgres

Each test session creates a throwaway database and drops it afterwards. When the variable is
missing, database tests are skipped locally but fail in CI (``CI`` is set), so they can never be
silently skipped in the pipeline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg's async driver cannot use Windows' default Proactor loop. Deployed code runs on
    # Linux, so this only matters for running the tests on a Windows machine.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def _admin_url() -> str:
    url = os.environ.get("VBALL_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("VBALL_TEST_DATABASE_URL must be set in CI")
        pytest.skip("VBALL_TEST_DATABASE_URL not set")
    return url


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a freshly created, empty database (no migrations applied)."""
    admin = make_url(_admin_url())
    name = f"vball_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def migrated_url(database_url: str) -> str:
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture(scope="session")
def engine(migrated_url: str) -> Iterator[Engine]:
    eng = create_engine(migrated_url)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after the test."""
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()
