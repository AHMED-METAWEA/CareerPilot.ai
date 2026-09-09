"""The ORM mapping and the migrated database must agree.

The migrations are the source of truth for DDL; `app/db/models.py` is the
runtime mapping. Drift between them is silent until a query fails in
production, so it is asserted here and by `alembic check` in CI.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import Base

pytestmark = pytest.mark.db


def test_every_mapped_table_exists(db_session: Session) -> None:
    inspector = sa.inspect(db_session.get_bind())
    actual = set(inspector.get_table_names())
    missing = {name for name in Base.metadata.tables if name not in actual}
    assert not missing, f"tables missing from the database: {sorted(missing)}"


def test_every_mapped_column_exists(db_session: Session) -> None:
    inspector = sa.inspect(db_session.get_bind())
    problems: list[str] = []
    for name, table in Base.metadata.tables.items():
        actual = {c["name"] for c in inspector.get_columns(name)}
        for column in table.columns:
            if column.name not in actual:
                problems.append(f"{name}.{column.name}")
    assert not problems, f"columns missing from the database: {problems}"


def test_extensions_are_installed(db_session: Session) -> None:
    installed = set(db_session.execute(sa.text("SELECT extname FROM pg_extension")).scalars().all())
    assert {"pgcrypto", "citext", "vector", "pg_trgm"} <= installed


def test_raw_payloads_is_partitioned(db_session: Session) -> None:
    """Pruning 90-day-old payloads must be a DROP, not a DELETE (§6.4)."""
    kind = db_session.execute(
        sa.text("SELECT relkind FROM pg_class WHERE relname = 'raw_payloads'")
    ).scalar_one()
    assert kind == "p"

    partitions = (
        db_session.execute(
            sa.text(
                """
            SELECT c.relname FROM pg_class c
              JOIN pg_inherits i ON i.inhrelid = c.oid
             WHERE i.inhparent = 'raw_payloads'::regclass
            """
            )
        )
        .scalars()
        .all()
    )
    assert any(p.startswith("raw_payloads_2") for p in partitions)
    assert "raw_payloads_default" in partitions, "an unroutable payload must never be lost"


def test_index_set_matches_the_specification(db_session: Session) -> None:
    """The indexes §6.3 names, including the two the ORM cannot express."""
    indexes = set(
        db_session.execute(sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
        .scalars()
        .all()
    )
    assert {"job_fts_idx", "job_vec_idx", "ix_matches_user_score"} <= indexes


def test_embedding_column_is_halfvec_384(db_session: Session) -> None:
    """halfvec halves index memory; 384 is multilingual-e5-small's dimension."""
    type_name = db_session.execute(
        sa.text(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
              FROM pg_attribute a
             WHERE a.attrelid = 'job_embeddings'::regclass AND a.attname = 'embedding'
            """
        )
    ).scalar_one()
    assert type_name == "halfvec(384)"
