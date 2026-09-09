"""Operator CLI (`careerpilot …`).

The operator's whole Phase 0 interface: sync the registry, run a source, read
the corpus against its exit criteria. Worth testing — an operator tool that
breaks silently is discovered at the worst moment.
"""

from __future__ import annotations

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app
from tests.conftest import load_fixture

pytestmark = pytest.mark.db

runner = CliRunner()


def test_sources_sync_loads_the_committed_registry(db_session: Session) -> None:
    result = runner.invoke(app, ["sources", "sync"])
    assert result.exit_code == 0, result.output
    assert "synced" in result.output

    count = db_session.execute(sa.text("SELECT count(*) FROM job_sources")).scalar_one()
    assert count > 100


def test_sources_list_on_an_empty_registry(db_session: Session) -> None:
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "no sources registered" in result.output


def test_sources_list_reports_health(db_session: Session, source_row) -> None:
    source_row()
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "greenhouse:test" in result.output


def test_sources_run_rejects_an_unknown_source(db_session: Session) -> None:
    result = runner.invoke(app, ["sources", "run", "greenhouse:nope"])
    assert result.exit_code == 1
    assert "no such source" in result.output


@respx.mock
def test_sources_run_ingests(db_session: Session, source_row) -> None:
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse", "board.json"))
    )
    source_row()
    result = runner.invoke(app, ["sources", "run", "greenhouse:test"])
    assert result.exit_code == 0, result.output
    assert "fetched=2 new=2" in result.output


def test_stats_reports_the_exit_criteria(db_session: Session) -> None:
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    for criterion in (
        "unique live postings",
        "apply_url",
        "false merges",
        "ambiguous apply URLs",
    ):
        assert criterion in result.output


def test_boards_probe_rejects_an_unknown_adapter() -> None:
    result = runner.invoke(app, ["boards", "probe", "linkedin", "acme"])
    assert result.exit_code == 1
    assert "unknown adapter" in result.output


@respx.mock
def test_boards_probe_prints_normalised_postings() -> None:
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse", "board.json"))
    )
    result = runner.invoke(app, ["boards", "probe", "greenhouse", "vercel", "--show", "1"])
    assert result.exit_code == 0, result.output
    assert "2 postings" in result.output
    assert "apply_url" in result.output
