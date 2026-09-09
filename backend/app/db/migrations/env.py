"""Alembic environment.

The database URL comes from application settings, never from alembic.ini, so
migrations and the running application can never disagree about the target.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


# Expression indexes Alembic cannot compare faithfully: Postgres rewrites the
# reflected expression (casts, quoting), so autogenerate reports a spurious
# drop-and-recreate on every run. They are created in migration bd890a7822e8
# and asserted by tests/integration/test_schema_parity.py instead.
UNCOMPARABLE_INDEXES = {"job_fts_idx"}


def include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Skip raw_payloads partitions: they are managed by the pruning job, not by DDL."""
    if type_ == "table" and name and name.startswith("raw_payloads_"):
        return False
    return not (type_ == "index" and name in UNCOMPARABLE_INDEXES)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
