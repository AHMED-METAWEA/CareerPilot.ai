"""Apply-URL liveness verification (§11.5).

The distinctions under test are the ones that matter to a user: `gone` means
the role is over, `blocked` means we could not check, and neither is allowed to
masquerade as the other.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient
from app.config import get_config
from app.domain.models import UrlStatus
from app.services.verification import VerificationOutcome, VerificationService

pytestmark = pytest.mark.db

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/1"
LIVE_PAGE = """
<html><head><title>Data Engineer at Acme</title></head>
<body><h1>Data Engineer</h1><p>Acme is hiring a data engineer.</p>
<a href="/apply">Apply now</a></body></html>
"""
CLOSED_PAGE = """
<html><body><h1>Data Engineer</h1>
<p>This job is no longer available. Acme thanks you for your interest.</p></body></html>
"""


@pytest.fixture
def posting(db_session: Session) -> uuid.UUID:
    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', 'greenhouse:acme', '{}'::jsonb) RETURNING id"
        )
    ).scalar_one()
    company_id = db_session.execute(
        sa.text("INSERT INTO companies (canonical_name) VALUES ('Acme') RETURNING id")
    ).scalar_one()
    return db_session.execute(
        sa.text(
            """
            INSERT INTO job_postings (source_id, external_id, company_id, title,
                title_normalized, description_text, apply_url, source_url, canonical_url, status)
            VALUES (:source_id, '1', :company_id, 'Data Engineer', 'data engineer', 'body',
                    :url, :url, :url, 'open')
            RETURNING id
            """
        ),
        {"source_id": source_id, "company_id": company_id, "url": APPLY_URL},
    ).scalar_one()


@pytest.fixture
def service(db_session: Session) -> VerificationService:
    http = HttpClient(
        user_agent="CareerPilotBot/test", per_host_min_interval_seconds=0.0, max_retries=1
    )
    return VerificationService(db_session, http, get_config())


def allow_robots() -> None:
    respx.get("https://boards.greenhouse.io/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )


def status_of(session: Session, posting_id: uuid.UUID) -> str:
    return session.execute(
        sa.text("SELECT url_status FROM job_postings WHERE id = :id"), {"id": posting_id}
    ).scalar_one()


@respx.mock
def test_live_posting(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    allow_robots()
    respx.get(APPLY_URL).mock(return_value=httpx.Response(200, html=LIVE_PAGE))

    report = service.verify_batch(limit=5)
    db_session.commit()

    assert report.live == 1
    assert status_of(db_session, posting) == UrlStatus.LIVE.value


@respx.mock
def test_404_marks_gone_and_expires_the_posting(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    allow_robots()
    respx.get(APPLY_URL).mock(return_value=httpx.Response(404))

    service.verify_batch(limit=5)
    db_session.commit()

    row = db_session.execute(
        sa.text("SELECT url_status, status FROM job_postings WHERE id = :id"), {"id": posting}
    ).one()
    assert row.url_status == UrlStatus.GONE.value
    assert row.status == "expired"


@respx.mock
def test_closure_phrase_marks_gone_despite_a_200(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    """A 200 with 'no longer available' is a closed role, not a live one."""
    allow_robots()
    respx.get(APPLY_URL).mock(return_value=httpx.Response(200, html=CLOSED_PAGE))

    service.verify_batch(limit=5)
    db_session.commit()
    assert status_of(db_session, posting) == UrlStatus.GONE.value


@respx.mock
def test_403_is_blocked_not_gone(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    """We could not check. That is not evidence the job has closed."""
    allow_robots()
    respx.get(APPLY_URL).mock(return_value=httpx.Response(403))

    service.verify_batch(limit=5)
    db_session.commit()

    row = db_session.execute(
        sa.text("SELECT url_status, status FROM job_postings WHERE id = :id"), {"id": posting}
    ).one()
    assert row.url_status == UrlStatus.BLOCKED.value
    assert row.status == "open", "a posting we cannot check is not a posting that ended"


@respx.mock
def test_robots_disallow_blocks_without_fetching(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    respx.get("https://boards.greenhouse.io/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    page = respx.get(APPLY_URL).mock(return_value=httpx.Response(200, html=LIVE_PAGE))

    service.verify_batch(limit=5)
    db_session.commit()

    assert page.call_count == 0, "robots.txt is honoured before the request, not after"
    assert status_of(db_session, posting) == UrlStatus.BLOCKED.value


@respx.mock
def test_a_page_about_neither_the_role_nor_the_employer_is_a_redirect(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    """200 on a generic careers index usually means a silent redirect."""
    allow_robots()
    respx.get(APPLY_URL).mock(
        return_value=httpx.Response(200, html="<html><body>Careers portal</body></html>")
    )

    service.verify_batch(limit=5)
    db_session.commit()
    assert status_of(db_session, posting) == UrlStatus.REDIRECTED.value


@respx.mock
def test_unreachable_host_leaves_status_unknown(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    allow_robots()
    respx.get(APPLY_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    service.verify_batch(limit=5)
    db_session.commit()
    assert status_of(db_session, posting) == UrlStatus.UNKNOWN.value


@respx.mock
def test_recently_verified_postings_are_not_rechecked(
    db_session: Session, posting: uuid.UUID, service: VerificationService
) -> None:
    allow_robots()
    route = respx.get(APPLY_URL).mock(return_value=httpx.Response(200, html=LIVE_PAGE))

    service.verify_batch(limit=5)
    db_session.commit()
    service.verify_batch(limit=5)
    db_session.commit()

    assert route.call_count == 1


# ── Lock contention (§11.5) ───────────────────────────────────────────


def test_each_posting_is_committed_before_the_next_is_fetched(
    db_session: Session, http_client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying 60 URLs takes minutes of network I/O.

    Holding one transaction across all of it kept a row lock on every posting
    for the whole batch, so discovery and detail-fetching queued behind it until
    Postgres cancelled somebody on `statement_timeout`. When the loser was this
    task it died — and since the chain only re-enqueues itself on success,
    verification stopped entirely until the next scheduled trigger.
    """
    service = VerificationService(db_session, http_client, get_config())
    committed: list[int] = []
    verified: list[int] = []

    real_commit = db_session.commit
    monkeypatch.setattr(
        db_session, "commit", lambda: (committed.append(len(verified)), real_commit())[1]
    )

    def fake_verify_one(**kwargs: object) -> VerificationOutcome:
        verified.append(1)
        return VerificationOutcome(
            kwargs["posting_id"],  # type: ignore[arg-type]
            UrlStatus.LIVE,
            detail={"status": 200},
        )

    monkeypatch.setattr(service, "verify_one", fake_verify_one)
    report = service.verify_batch(limit=3)

    assert report.checked == len(committed), (
        "one commit per posting, so a lock is held for a write rather than for a batch"
    )
    assert committed == list(range(1, report.checked + 1)), (
        "commits must interleave with verifications, not all land at the end"
    )
