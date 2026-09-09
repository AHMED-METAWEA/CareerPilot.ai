"""Discovery runs end to end against a real database (§11.2 W2)."""

from __future__ import annotations

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient, SourceUnavailableError
from app.config import get_config
from app.services.ingestion import IngestionService
from tests.conftest import load_fixture

pytestmark = pytest.mark.db

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/vercel/jobs"


def service(session: Session, http: HttpClient) -> IngestionService:
    return IngestionService(session, http, get_config())


def mock_board(payload: object) -> None:
    respx.get(url__startswith=GREENHOUSE_URL).mock(return_value=httpx.Response(200, json=payload))


@respx.mock
def test_run_persists_postings_and_raw_payloads(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    mock_board(load_fixture("greenhouse", "board.json"))
    source_id = source_row()

    report = service(db_session, http_client).run_source(source_id)
    db_session.commit()

    assert report.fetched == 2 and report.new == 2 and report.errors == 0
    assert report.status == "ok"

    postings = db_session.execute(
        sa.text(
            "SELECT apply_url, canonical_url, source_url, ats_platform, ats_confidence, "
            "detection_method, company_id, content_simhash FROM job_postings"
        )
    ).all()
    assert len(postings) == 2
    for row in postings:
        # Phase 0 exit criterion: every row has a populated apply_url.
        assert row.apply_url.startswith("https://")
        assert row.canonical_url and row.source_url
        assert (row.ats_platform, float(row.ats_confidence), row.detection_method) == (
            "greenhouse",
            1.0,
            "construction",
        )
        assert row.company_id is not None
        assert row.content_simhash is not None

    # The payload is stored verbatim, before anything interprets it.
    raw_count = db_session.execute(sa.text("SELECT count(*) FROM raw_payloads")).scalar_one()
    assert raw_count == 2


@respx.mock
def test_rerunning_a_source_updates_in_place(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """Dedup stage 1: (source_id, external_id) is the same posting."""
    mock_board(load_fixture("greenhouse", "board.json"))
    source_id = source_row()
    svc = service(db_session, http_client)

    svc.run_source(source_id)
    db_session.commit()
    second = svc.run_source(source_id)
    db_session.commit()

    assert (second.new, second.updated) == (0, 2)
    assert db_session.execute(sa.text("SELECT count(*) FROM job_postings")).scalar_one() == 2


@respx.mock
def test_an_update_never_blanks_an_existing_description(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """A list-only row from a two-phase source must not erase a fetched body."""
    payload = load_fixture("greenhouse", "board.json")
    mock_board(payload)
    source_id = source_row()
    svc = service(db_session, http_client)
    svc.run_source(source_id)
    db_session.commit()

    stripped = {"jobs": [{**job, "content": ""} for job in payload["jobs"]]}
    respx.get(url__startswith=GREENHOUSE_URL).mock(return_value=httpx.Response(200, json=stripped))
    svc.run_source(source_id)
    db_session.commit()

    lengths = (
        db_session.execute(sa.text("SELECT length(description_text) FROM job_postings"))
        .scalars()
        .all()
    )
    assert all(length > 0 for length in lengths)


@respx.mock
def test_normalisation_failure_costs_one_row_not_the_run(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    payload = load_fixture("greenhouse", "board.json")
    broken = {"jobs": [payload["jobs"][0], {**payload["jobs"][1], "title": None}]}
    mock_board(broken)
    source_id = source_row()

    report = service(db_session, http_client).run_source(source_id)
    db_session.commit()

    assert report.fetched == 2 and report.errors == 1 and report.new == 1
    # The unusable payload is still stored, so the fix is a re-run, not a re-fetch.
    assert db_session.execute(sa.text("SELECT count(*) FROM raw_payloads")).scalar_one() == 2


@respx.mock
def test_company_is_created_once_and_reused(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    mock_board(load_fixture("greenhouse", "board.json"))
    source_id = source_row()
    service(db_session, http_client).run_source(source_id)
    db_session.commit()

    companies = db_session.execute(sa.text("SELECT canonical_name FROM companies")).all()
    assert len(companies) == 1 and companies[0].canonical_name == "Vercel"


@respx.mock
def test_ambiguous_employer_goes_to_the_review_queue(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """A subsidiary-shaped name gets its own row *and* a human review entry (R5)."""
    db_session.execute(
        sa.text("INSERT INTO companies (canonical_name, domain) VALUES ('Vercel', 'vercel.com')")
    )
    db_session.commit()

    mock_board(load_fixture("greenhouse", "board.json"))
    source_id = source_row(
        name="greenhouse:vercel-eg", board_token="vercel", company_name="Vercel Egypt"
    )
    service(db_session, http_client).run_source(source_id)
    db_session.commit()

    queued = db_session.execute(
        sa.text(
            "SELECT observed_name, matched_alias, suggested_company_id FROM company_review_queue"
        )
    ).all()
    assert len(queued) == 1
    assert queued[0].observed_name == "Vercel Egypt"
    assert queued[0].matched_alias == "Vercel"
    assert queued[0].suggested_company_id is not None
    # The posting still has an employer: nothing is left dangling.
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM job_postings WHERE company_id IS NULL")
        ).scalar_one()
        == 0
    )


@respx.mock
def test_zero_rows_against_a_productive_history_is_suspect(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """R2: the failure mode that is invisible without per-source health."""
    source_id = source_row()
    for _ in range(3):
        db_session.execute(
            sa.text(
                """
                INSERT INTO source_runs (source_id, started_at, finished_at, fetched, status)
                VALUES (:id, now() - interval '1 day', now() - interval '1 day', 120, 'ok')
                """
            ),
            {"id": source_id},
        )
    db_session.commit()

    mock_board({"jobs": []})
    report = service(db_session, http_client).run_source(source_id)
    db_session.commit()

    assert report.fetched == 0
    assert report.status == "suspect"
    assert report.detail["median_7d"] == 120


@respx.mock
def test_circuit_breaker_disables_a_source_after_five_failures(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """Five consecutive failures stop the source instead of retrying into a quota."""
    respx.get(url__startswith=GREENHOUSE_URL).mock(return_value=httpx.Response(500))
    source_id = source_row()
    svc = service(db_session, http_client)

    for _ in range(5):
        with pytest.raises(SourceUnavailableError):
            svc.run_source(source_id)
        db_session.commit()

    row = db_session.execute(
        sa.text(
            "SELECT enabled, consecutive_failures, disabled_reason FROM job_sources WHERE id = :id"
        ),
        {"id": source_id},
    ).one()
    assert row.enabled is False
    assert row.consecutive_failures >= 5
    assert "circuit breaker" in row.disabled_reason


@respx.mock
def test_a_successful_run_resets_the_failure_counter(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    source_id = source_row()
    db_session.execute(
        sa.text("UPDATE job_sources SET consecutive_failures = 3 WHERE id = :id"),
        {"id": source_id},
    )
    db_session.commit()

    mock_board(load_fixture("greenhouse", "board.json"))
    service(db_session, http_client).run_source(source_id)
    db_session.commit()

    assert (
        db_session.execute(
            sa.text("SELECT consecutive_failures FROM job_sources WHERE id = :id"),
            {"id": source_id},
        ).scalar_one()
        == 0
    )


@respx.mock
def test_two_phase_source_queues_detail_fetches(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    respx.get(
        url__startswith="https://api.smartrecruiters.com/v1/companies/McDonaldsCorporation/postings"
    ).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters", "board.json")))
    source_id = source_row(
        adapter="smartrecruiters",
        name="smartrecruiters:mcd",
        company_id="McDonaldsCorporation",
        company_name="McDonald's",
    )

    report = service(db_session, http_client).run_source(source_id)
    db_session.commit()

    assert report.new == 2
    assert report.detail["details_queued"] == 2
    queued = (
        db_session.execute(
            sa.text("SELECT payload FROM task_queue WHERE task_type = 'fetch_details'")
        )
        .scalars()
        .all()
    )
    assert queued and len(queued[0]["external_ids"]) == 2


@respx.mock
def test_two_boards_on_the_same_ats_create_two_companies(
    db_session: Session, source_row, http_client: HttpClient
) -> None:
    """Regression for the false merge found during the first corpus build.

    Greenhouse job URLs live on a host shared by every Greenhouse customer. Using
    one as the employer's careers URL made every board resolve to the domain
    `greenhouse.io`, and `ON CONFLICT (domain)` then folded thirteen unrelated
    employers into a single company row with 937 postings.
    """
    payload = load_fixture("greenhouse", "board.json")
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(200, json=payload)
    )
    svc = service(db_session, http_client)
    svc.run_source(
        source_row(name="greenhouse:vercel", board_token="vercel", company_name="Vercel")
    )
    svc.run_source(source_row(name="greenhouse:adyen", board_token="adyen", company_name="Adyen"))
    db_session.commit()

    names = db_session.execute(
        sa.text("SELECT canonical_name, domain FROM companies ORDER BY canonical_name")
    ).all()
    assert [row.canonical_name for row in names] == ["Adyen", "Vercel"]
    # No employer carries an ATS host as its domain.
    assert all(row.domain is None or "greenhouse" not in row.domain for row in names)


@respx.mock
def test_repeated_employer_is_resolved_once_per_run(
    db_session: Session, source_row, http_client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board is one employer repeated hundreds of times.

    Matching is a full scan of the company directory, so resolving per posting
    makes a run O(postings x companies) fuzzy comparisons for one answer.
    """
    import app.services.companies as companies_module

    calls = 0
    original = companies_module.resolve_company

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(companies_module, "resolve_company", counted)

    respx.get(url__startswith=GREENHOUSE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse", "board.json"))
    )
    report = service(db_session, http_client).run_source(source_row())
    db_session.commit()

    assert report.new == 2
    assert calls == 1, "the second posting reused the resolution from the first"
