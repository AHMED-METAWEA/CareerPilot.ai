"""The matching funnel against a real database (§11.4).

Built on a small, controlled corpus rather than the live one: the point is to
pin behaviour — which postings pass the gates, what the evidence says, how the
score decomposes — and that needs data whose right answer is known.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.embeddings import build_embedder
from app.adapters.embeddings.deterministic import DeterministicReranker
from app.config import BASE_DIR, get_config
from app.services.embedding import EmbeddingService
from app.services.matching import MODEL_VERSION, MatchingService
from app.services.taxonomy import load_seed, sync_skills

pytestmark = pytest.mark.db

DATA_ENGINEER_JD = """We are hiring a Data Engineer for our platform team.

Requirements
• 3+ years building data pipelines in Python
• Strong experience with Apache Kafka and streaming systems
• Comfortable with SQL and PostgreSQL

Nice to have
• Experience with Apache Airflow
"""

NURSE_JD = """We are hiring a Registered Nurse for our paediatric ward.

Requirements
• Valid nursing registration
• 2+ years of ward experience
• Comfortable with shift work including nights
"""


@pytest.fixture
def corpus(db_session: Session) -> dict[str, uuid.UUID]:
    sync_skills(db_session, load_seed(BASE_DIR / "config" / "skills.yaml"))
    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', 'greenhouse:test', '{}'::jsonb) RETURNING id"
        )
    ).scalar_one()
    company_id = db_session.execute(
        sa.text("INSERT INTO companies (canonical_name) VALUES ('Acme') RETURNING id")
    ).scalar_one()

    now = datetime.now(UTC)
    ids: dict[str, uuid.UUID] = {}
    postings = [
        ("data_engineer", "Data Engineer", DATA_ENGINEER_JD, "remote", None, now, "live", now),
        ("nurse", "Registered Nurse", NURSE_JD, "remote", None, now, "live", now),
        ("onsite_berlin", "Data Engineer", DATA_ENGINEER_JD, "onsite", "DE", now, "live", now),
        (
            "stale",
            "Data Engineer",
            DATA_ENGINEER_JD,
            "remote",
            None,
            now - timedelta(days=90),
            "live",
            now,
        ),
        ("unverified", "Data Engineer", DATA_ENGINEER_JD, "remote", None, now, "unknown", None),
    ]
    for key, title, body, remote, country, posted, url_status, verified in postings:
        locations = json.dumps(
            [
                {
                    "raw": country or "Remote",
                    "city": None,
                    "country": country,
                    "is_remote": country is None,
                }
            ]
        )
        ids[key] = db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, company_id, title,
                    title_normalized, description_text, locations, remote_type, apply_url,
                    source_url, canonical_url, posted_at, url_status, last_verified_at, status)
                VALUES (:source_id, :external_id, :company_id, :title, lower(:title), :body,
                        CAST(:locations AS jsonb), :remote, :url, :url, :url, :posted,
                        :url_status, :verified, 'open')
                RETURNING id
                """
            ),
            {
                "source_id": source_id,
                "external_id": key,
                "company_id": company_id,
                "title": title,
                "body": body,
                "locations": locations,
                "remote": remote,
                "url": f"https://boards.greenhouse.io/acme/jobs/{key}",
                "posted": posted,
                "url_status": url_status,
                "verified": verified,
            },
        ).scalar_one()

    EmbeddingService(db_session, build_embedder("deterministic")).embed_pending(batch_size=50)
    db_session.commit()
    return ids


@pytest.fixture
def profile_id(db_session: Session) -> uuid.UUID:
    user_id = db_session.execute(
        sa.text("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id"),
        {"e": f"{uuid.uuid4().hex[:8]}@example.com"},
    ).scalar_one()
    # A profile is always attached to the CV version it was extracted from, so
    # every claim stays traceable to a document.
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:user_id, 'k', 'text/plain', :sha) RETURNING id"
        ),
        {"user_id": user_id, "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    cv_version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:doc, 1, 'cv text', 0.9, 'test') RETURNING id"
        ),
        {"doc": document_id},
    ).scalar_one()
    identifier = db_session.execute(
        sa.text(
            """
            INSERT INTO candidate_profiles (user_id, cv_version_id, years_experience,
                                            seniority_level, locations, work_auth, languages)
            VALUES (:user_id, :cv_version_id, 5, 'mid', ARRAY['Cairo, Egypt'],
                    '{"EG": "citizen"}'::jsonb, '[{"lang": "English", "cefr": "C1"}]'::jsonb)
            RETURNING id
            """
        ),
        {"user_id": user_id, "cv_version_id": cv_version_id},
    ).scalar_one()

    for name in ("Python", "Apache Kafka", "SQL"):
        skill_id = db_session.execute(
            sa.text("SELECT id FROM skills WHERE canonical_name = :name"), {"name": name}
        ).scalar_one()
        db_session.execute(
            sa.text(
                "INSERT INTO profile_skills (profile_id, skill_id, evidence_span, source) "
                "VALUES (:profile_id, :skill_id, '[0,10)'::int4range, 'extracted')"
            ),
            {"profile_id": identifier, "skill_id": skill_id},
        )

    embedder = build_embedder("deterministic")
    bullets = [
        "Built streaming data pipelines in Python and Apache Kafka processing millions of events",
        "Wrote SQL reporting and PostgreSQL models for network operations",
        "Orchestrated nightly jobs with Apache Airflow",
    ]
    for ordinal, (bullet, vector) in enumerate(zip(bullets, embedder.encode(bullets), strict=True)):
        db_session.execute(
            sa.text(
                "INSERT INTO profile_bullets (profile_id, ordinal, text, model, embedding) "
                "VALUES (:profile_id, :ordinal, :text, :model, :embedding)"
            ),
            {
                "profile_id": identifier,
                "ordinal": ordinal,
                "text": bullet,
                "model": embedder.model_id,
                "embedding": "[" + ",".join(f"{value:.6f}" for value in vector) + "]",
            },
        )
    db_session.commit()
    return identifier


def service(db_session: Session) -> MatchingService:
    return MatchingService(
        db_session,
        get_config(),
        embedder=build_embedder("deterministic"),
        reranker=DeterministicReranker(),
    )


def test_gates_hold_back_what_the_candidate_cannot_take(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    report = service(db_session).run_for_profile(profile_id)
    db_session.commit()

    assert report.eligible == 2
    assert report.gate_failures["location"] == 1
    assert report.gate_failures["unverified_url"] == 1


def test_stale_postings_are_filtered_before_the_gates_see_them(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    """The freshness window is applied in SQL, not by loading the corpus.

    A consequence worth stating: a posting older than the window never reaches
    the gates, so it is not listed as "withheld for freshness" — it is simply
    out of scope. The freshness gate stays as defence in depth for callers that
    assemble their own pool. Both use `gates.max_posting_age_days`, so they
    cannot disagree.
    """
    report = service(db_session).run_for_profile(profile_id)
    db_session.commit()

    assert report.scanned == 4, "the 90-day-old posting was excluded in SQL"
    assert "freshness" not in report.gate_failures
    withheld = db_session.execute(
        sa.text("SELECT count(*) FROM matches WHERE posting_id = :id"),
        {"id": corpus["stale"]},
    ).scalar_one()
    assert withheld == 0


def test_the_relevant_role_outranks_the_irrelevant_one(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    """§9.4's cross-domain negative control, in miniature: a data engineer's CV
    must separate a data engineering role from a nursing one."""
    service(db_session).run_for_profile(profile_id)
    db_session.commit()

    rows = db_session.execute(
        sa.text(
            "SELECT p.title, m.total_score FROM matches m JOIN job_postings p ON p.id = m.posting_id "
            "WHERE m.gate_passed ORDER BY m.total_score DESC"
        )
    ).all()
    assert [row.title for row in rows] == ["Data Engineer", "Registered Nurse"]
    assert float(rows[0].total_score) > float(rows[1].total_score)


def test_requirements_and_evidence_are_persisted(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    service(db_session).run_for_profile(profile_id)
    db_session.commit()

    requirements = db_session.execute(
        sa.text(
            "SELECT text, is_must_have FROM job_requirements WHERE posting_id = :id ORDER BY text"
        ),
        {"id": corpus["data_engineer"]},
    ).all()
    assert any("Kafka" in row.text for row in requirements)
    assert any(row.is_must_have for row in requirements)
    assert any(not row.is_must_have for row in requirements)  # the nice-to-have

    evidence = db_session.execute(
        sa.text(
            """
            SELECT e.status, e.similarity, e.note FROM match_evidence e
              JOIN matches m ON m.id = e.match_id
             WHERE m.posting_id = :id
            """
        ),
        {"id": corpus["data_engineer"]},
    ).all()
    assert evidence, "every scored requirement should leave an evidence row"


def test_skill_coverage_is_reported_and_gaps_are_named(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    service(db_session).run_for_profile(profile_id)
    db_session.commit()

    row = db_session.execute(
        sa.text(
            "SELECT explanation, gaps, subscores FROM matches "
            "WHERE posting_id = :id AND gate_passed"
        ),
        {"id": corpus["data_engineer"]},
    ).one()
    assert "skill requirements" in row.explanation
    assert "roles reviewed for you" in row.explanation
    # Never a probability of an outcome (ADR 0006).
    for phrase in ("chance", "probability", "guaranteed"):
        assert phrase not in row.explanation.casefold()
    assert set(row.subscores) >= {"skill_coverage", "freshness", "contributions"}


def test_withheld_postings_record_their_reasons(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    service(db_session).run_for_profile(profile_id)
    db_session.commit()

    row = db_session.execute(
        sa.text("SELECT gate_failures FROM matches WHERE posting_id = :id"),
        {"id": corpus["onsite_berlin"]},
    ).one()
    assert "location" in row.gate_failures


def test_rerunning_is_idempotent(
    db_session: Session, corpus: dict[str, uuid.UUID], profile_id: uuid.UUID
) -> None:
    """(profile_id, posting_id, model_version) is unique by construction (§11.4)."""
    service(db_session).run_for_profile(profile_id)
    db_session.commit()
    before = db_session.execute(sa.text("SELECT count(*) FROM matches")).scalar_one()

    service(db_session).run_for_profile(profile_id)
    db_session.commit()
    after = db_session.execute(sa.text("SELECT count(*) FROM matches")).scalar_one()

    assert before == after
    assert (
        db_session.execute(
            sa.text("SELECT count(DISTINCT model_version) FROM matches")
        ).scalar_one()
        == 1
    )
    assert MODEL_VERSION
