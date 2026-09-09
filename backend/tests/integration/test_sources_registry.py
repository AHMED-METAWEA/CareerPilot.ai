"""Board registry: YAML in Git, rows in Postgres (§5.7).

`config/boards/*.yaml` is operator-edited data, so the loader has to reject the
mistakes an operator actually makes — a typo'd adapter, a duplicated source
name — before they reach the database.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.services.sources import BoardEntry, load_boards, source_health, sync_boards

pytestmark = pytest.mark.db


def write_boards(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(textwrap.dedent(body), encoding="utf-8")


def test_shipped_registry_loads(tmp_path: Path) -> None:
    boards = load_boards(Path("config/boards"))
    assert len(boards) > 100
    assert all(entry.name and entry.config for entry in boards)
    # Region comes from the filename, so a board is always attributable.
    assert {entry.region for entry in boards} <= {"egypt", "mena", "eu_remote", "global_remote"}


def test_unknown_adapter_is_rejected(tmp_path: Path) -> None:
    write_boards(
        tmp_path,
        "x.yaml",
        """
        - adapter: linkedin
          name: linkedin:acme
          config: {board_token: acme}
        """,
    )
    with pytest.raises(ValueError, match="unknown adapter"):
        load_boards(tmp_path)


def test_duplicate_source_name_is_rejected(tmp_path: Path) -> None:
    """Source names are unique across the whole registry, not per file."""
    write_boards(
        tmp_path,
        "a.yaml",
        """
        - adapter: greenhouse
          name: greenhouse:acme
          config: {board_token: acme}
        """,
    )
    write_boards(
        tmp_path,
        "b.yaml",
        """
        - adapter: greenhouse
          name: greenhouse:acme
          config: {board_token: acme}
        """,
    )
    with pytest.raises(ValueError, match="duplicate source name"):
        load_boards(tmp_path)


def test_sync_creates_updates_and_disables(db_session: Session) -> None:
    first = [
        BoardEntry(adapter="greenhouse", name="greenhouse:a", config={"board_token": "a"}),
        BoardEntry(adapter="lever", name="lever:b", config={"company": "b"}),
    ]
    report = sync_boards(db_session, first)
    db_session.commit()
    assert (report.created, report.updated, report.disabled) == (2, 0, 0)

    second = [
        BoardEntry(
            adapter="greenhouse",
            name="greenhouse:a",
            config={"board_token": "a2"},
            rate_limit_rpm=45,
        )
    ]
    report = sync_boards(db_session, second)
    db_session.commit()
    assert (report.created, report.updated) == (0, 1)

    rows = dict(
        db_session.execute(sa.text("SELECT name, enabled FROM job_sources")).all()  # type: ignore[arg-type]
    )
    # A board removed from YAML is disabled, never deleted: its postings still
    # reference the source row.
    assert rows == {"greenhouse:a": True, "lever:b": False}
    assert (
        db_session.execute(
            sa.text("SELECT config->>'board_token' FROM job_sources WHERE name = 'greenhouse:a'")
        ).scalar_one()
        == "a2"
    )


def test_sync_preserves_operational_state(db_session: Session) -> None:
    """A re-sync must not reset a circuit breaker or force a full re-fetch."""
    entry = BoardEntry(adapter="greenhouse", name="greenhouse:a", config={"board_token": "a"})
    sync_boards(db_session, [entry])
    db_session.execute(
        sa.text(
            """
            UPDATE job_sources
               SET consecutive_failures = 3, etag = 'W/abc', last_run_at = now()
             WHERE name = 'greenhouse:a'
            """
        )
    )
    db_session.commit()

    sync_boards(db_session, [entry])
    db_session.commit()

    row = db_session.execute(
        sa.text("SELECT consecutive_failures, etag, last_run_at FROM job_sources")
    ).one()
    assert (row.consecutive_failures, row.etag) == (3, "W/abc")
    assert row.last_run_at is not None


def test_source_health_reports_an_empty_registry(db_session: Session) -> None:
    assert source_health(db_session) == []
