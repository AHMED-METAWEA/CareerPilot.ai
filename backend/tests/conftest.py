"""Shared fixtures.

Adapter tests replay payloads recorded from live ATS endpoints (see
`tests/fixtures/`). Nothing in the suite makes a network call: a test that
depends on a third party is a test that fails on their schedule.
"""

from __future__ import annotations

import json
import os
import uuid as _uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient
from app.config import get_config

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(*parts: str) -> Any:
    with (FIXTURES.joinpath(*parts)).open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def http_client() -> HttpClient:
    """A client with the throttle disabled — tests must not sleep."""
    config = get_config()
    return HttpClient(
        user_agent=config.ingestion.user_agent,
        timeout_seconds=5.0,
        max_retries=2,
        per_host_min_interval_seconds=0.0,
    )


# ── Database fixtures ─────────────────────────────────────────────────
#
# Tests marked `db` run against a real PostgreSQL with pgvector — the schema
# uses citext, int4range, halfvec, partitioning and SKIP LOCKED, none of which
# a SQLite stand-in would exercise. Without a reachable database they skip
# rather than fail, so the pure-domain suite still runs anywhere.

DEFAULT_TEST_DB = "postgresql+psycopg://careerpilot:careerpilot@localhost:5433/careerpilot_test"


def _test_database_url() -> str:
    return os.environ.get("CAREERPILOT_TEST_DATABASE_URL", DEFAULT_TEST_DB)


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create the test database and migrate it to head, or skip the db suite."""
    url = _test_database_url()
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    database = url.rsplit("/", 1)[1]

    try:
        engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as connection:
            exists = connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
            ).first()
            if not exists:
                connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        engine.dispose()
    except sa.exc.OperationalError as exc:  # no server, no db tests
        pytest.skip(f"PostgreSQL unavailable at {admin_url}: {exc}")

    os.environ["DATABASE_URL"] = url

    from alembic import command
    from alembic.config import Config

    from app import config as config_module
    from app.config import BASE_DIR
    from app.db import session as session_module

    config_module.reset_config_cache()
    session_module.reset_engine_cache()

    alembic_config = Config(str(BASE_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BASE_DIR / "app" / "db" / "migrations"))
    command.upgrade(alembic_config, "head")
    return url


@pytest.fixture
def db_session(database_url: str) -> Iterator[Session]:
    """A session on the test database, with every table truncated afterwards."""
    from app.db.session import get_engine, get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with get_engine().begin() as connection:
            tables = (
                connection.execute(
                    sa.text(
                        """
                    SELECT tablename FROM pg_tables
                     WHERE schemaname = 'public'
                       AND tablename NOT IN ('alembic_version')
                       AND tablename NOT LIKE 'raw_payloads_%'
                    """
                    )
                )
                .scalars()
                .all()
            )
            if tables:
                connection.execute(
                    sa.text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
                )


@pytest.fixture
def source_row(db_session: Session):
    """A registered Greenhouse source to ingest into."""

    def _create(
        adapter: str = "greenhouse", name: str | None = None, **config: object
    ) -> _uuid.UUID:
        import json

        row = db_session.execute(
            sa.text(
                """
                INSERT INTO job_sources (adapter, name, config, tier, rate_limit_rpm)
                VALUES (:adapter, :name, CAST(:config AS jsonb), 1, 600)
                RETURNING id
                """
            ),
            {
                "adapter": adapter,
                "name": name or f"{adapter}:test",
                "config": json.dumps(config or {"board_token": "vercel", "company_name": "Vercel"}),
            },
        ).one()
        db_session.commit()
        return row.id

    return _create
