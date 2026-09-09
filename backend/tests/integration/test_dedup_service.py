"""Deduplication against the database (§11.3 W3)."""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import get_config
from app.domain.dedup.simhash import simhash64, to_signed
from app.domain.jobs.normalize import html_to_text
from app.services.dedup import DedupService
from tests.conftest import load_fixture

pytestmark = pytest.mark.db

# A real recorded description. Synthetic filler made of one repeated sentence
# behaves quite differently under SimHash from natural prose, and calibrating
# against it would produce a threshold that does not hold on real postings.
BODY = html_to_text(load_fixture("greenhouse", "board.json")["jobs"][0]["content"])


def make_source(session: Session, name: str, tier: int = 1) -> uuid.UUID:
    return (
        session.execute(
            sa.text(
                """
            INSERT INTO job_sources (adapter, name, config, tier)
            VALUES ('greenhouse', :name, '{}'::jsonb, :tier) RETURNING id
            """
            ),
            {"name": name, "tier": tier},
        )
        .one()
        .id
    )


def make_company(session: Session, name: str = "Acme") -> uuid.UUID:
    return (
        session.execute(
            sa.text("INSERT INTO companies (canonical_name) VALUES (:name) RETURNING id"),
            {"name": name},
        )
        .one()
        .id
    )


def make_posting(
    session: Session,
    source_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    external_id: str,
    title: str = "senior backend engineer",
    body: str = BODY,
    url: str = "https://boards.greenhouse.io/acme/jobs/1",
    native: bool = True,
    city: str | None = "Cairo",
    country: str | None = "EG",
) -> uuid.UUID:
    locations = (
        []
        if city is None
        else [{"raw": f"{city}", "city": city, "country": country, "is_remote": False}]
    )
    return (
        session.execute(
            sa.text(
                """
            INSERT INTO job_postings (
                source_id, external_id, company_id, title, title_normalized,
                description_text, locations, apply_url, source_url, canonical_url,
                content_simhash, ats_platform, ats_confidence, detection_method, posted_at
            ) VALUES (
                :source_id, :external_id, :company_id, :title, :title,
                :body, CAST(:locations AS jsonb), :url, :url, :url,
                :simhash, 'greenhouse', :confidence, :method, now()
            ) RETURNING id
            """
            ),
            {
                "source_id": source_id,
                "external_id": external_id,
                "company_id": company_id,
                "title": title,
                "body": body,
                "locations": json.dumps(locations),
                "url": url,
                "simhash": to_signed(simhash64(body)),
                "confidence": 1.00 if native else 0.95,
                "method": "construction" if native else "url",
            },
        )
        .one()
        .id
    )


def dedup(session: Session, ids: list[uuid.UUID]):
    report = DedupService(session, get_config()).deduplicate(ids)
    session.commit()
    return report


def group_of(session: Session, posting_id: uuid.UUID) -> uuid.UUID | None:
    return session.execute(
        sa.text("SELECT job_group_id FROM job_postings WHERE id = :id"), {"id": posting_id}
    ).scalar_one()


def test_syndicated_copies_land_in_one_group(db_session: Session) -> None:
    company = make_company(db_session)
    ats = make_source(db_session, "greenhouse:acme")
    aggregator = make_source(db_session, "remotive", tier=2)
    native = make_posting(db_session, ats, company, external_id="1")
    copy = make_posting(
        db_session,
        aggregator,
        company,
        external_id="1",
        body=BODY + " Apply via our partner site. No agencies.",
        url="https://remotive.com/jobs/1",
        native=False,
    )
    db_session.commit()

    report = dedup(db_session, [native, copy])

    assert report.merged_postings == 2
    assert group_of(db_session, native) == group_of(db_session, copy)

    canonical = db_session.execute(
        sa.text("SELECT canonical_posting_id, member_count FROM job_groups")
    ).one()
    # The employer's own ATS posting is the one a user is sent to (stage 7).
    assert canonical.canonical_posting_id == native
    assert canonical.member_count == 2


def test_same_canonical_url_merges_even_with_different_titles(db_session: Session) -> None:
    company = make_company(db_session)
    a_source = make_source(db_session, "greenhouse:acme")
    b_source = make_source(db_session, "aggregator", tier=2)
    shared = "https://boards.greenhouse.io/acme/jobs/77"
    a = make_posting(db_session, a_source, company, external_id="77", url=shared)
    b = make_posting(
        db_session,
        b_source,
        company,
        external_id="x",
        title="backend engineer senior",
        url=shared,
        native=False,
    )
    db_session.commit()

    dedup(db_session, [a, b])
    assert group_of(db_session, a) == group_of(db_session, b)


def test_distinct_roles_stay_separate(db_session: Session) -> None:
    company = make_company(db_session)
    source = make_source(db_session, "greenhouse:acme")
    engineer = make_posting(db_session, source, company, external_id="1")
    nurse = make_posting(
        db_session,
        source,
        company,
        external_id="2",
        title="registered nurse",
        body="We are hiring a registered nurse for our paediatric ward. " * 8,
        url="https://boards.greenhouse.io/acme/jobs/2",
    )
    db_session.commit()

    dedup(db_session, [engineer, nurse])
    assert group_of(db_session, engineer) != group_of(db_session, nurse)


def test_same_role_in_two_cities_stays_separate(db_session: Session) -> None:
    """Identical text, different city: two jobs, and a candidate in one of them
    must not have the other silently substituted."""
    company = make_company(db_session)
    source = make_source(db_session, "greenhouse:acme")
    cairo = make_posting(db_session, source, company, external_id="1", city="Cairo", country="EG")
    dubai = make_posting(
        db_session,
        source,
        company,
        external_id="2",
        city="Dubai",
        country="AE",
        url="https://boards.greenhouse.io/acme/jobs/2",
    )
    db_session.commit()

    dedup(db_session, [cairo, dubai])
    assert group_of(db_session, cairo) != group_of(db_session, dubai)


def test_a_later_posting_joins_an_existing_group(db_session: Session) -> None:
    """Group ids must be stable: a re-syndication does not spawn a rival group."""
    company = make_company(db_session)
    ats = make_source(db_session, "greenhouse:acme")
    aggregator = make_source(db_session, "remotive", tier=2)
    first = make_posting(db_session, ats, company, external_id="1")
    db_session.commit()
    dedup(db_session, [first])
    original_group = group_of(db_session, first)

    later = make_posting(
        db_session,
        aggregator,
        company,
        external_id="1",
        body=BODY + " Posted via partner.",
        url="https://remotive.com/jobs/9",
        native=False,
    )
    db_session.commit()
    report = dedup(db_session, [later])

    assert report.groups_joined == 1 and report.groups_created == 0
    assert group_of(db_session, later) == original_group


def test_dedup_is_idempotent(db_session: Session) -> None:
    company = make_company(db_session)
    source = make_source(db_session, "greenhouse:acme")
    posting = make_posting(db_session, source, company, external_id="1")
    db_session.commit()

    dedup(db_session, [posting])
    group = group_of(db_session, posting)
    dedup(db_session, [posting])

    assert group_of(db_session, posting) == group
    assert db_session.execute(sa.text("SELECT count(*) FROM job_groups")).scalar_one() == 1


def test_board_page_urls_do_not_collapse_a_whole_board(db_session: Session) -> None:
    """Corpus-wide guard, complementing the in-pool one.

    A shared canonical URL may have its members spread across many dedup batches,
    so the pool alone cannot see how ambiguous it is.
    """
    company = make_company(db_session)
    source = make_source(db_session, "greenhouse:acme")
    board_url = "https://acme.com/careers/job"
    ids = [
        make_posting(
            db_session,
            source,
            company,
            external_id=str(i),
            title=f"role number {i}",
            body=BODY + f" Requisition {i}.",
            url=board_url,
        )
        for i in range(12)
    ]
    db_session.commit()

    report = dedup(db_session, ids)

    assert report.ambiguous_urls == 1
    groups = {group_of(db_session, pid) for pid in ids}
    assert len(groups) == 12, "a board-page URL must not merge distinct postings"
