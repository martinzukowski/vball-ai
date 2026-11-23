from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from conftest import alembic_config
from vball_core.db.models import ALL_TABLES

pytestmark = pytest.mark.db


def test_upgrade_downgrade_roundtrip_and_no_drift(database_url: str) -> None:
    cfg = alembic_config(database_url)
    engine = create_engine(database_url)
    try:
        command.upgrade(cfg, "head")
        tables = set(inspect(engine).get_table_names())
        assert set(ALL_TABLES) <= tables

        # Models and migrations describe the same schema (raises if autogenerate finds diffs).
        command.check(cfg)

        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}

        command.upgrade(cfg, "head")
        assert set(ALL_TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_row_level_security_enabled_on_every_table(migrated_url: str) -> None:
    engine = create_engine(migrated_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                    "AND relname <> 'alembic_version'"
                )
            ).all()
    finally:
        engine.dispose()
    rls = dict(rows)
    assert set(rls) == set(ALL_TABLES)
    assert all(rls.values()), {t: on for t, on in rls.items() if not on}
