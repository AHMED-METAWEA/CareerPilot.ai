"""Phase 1 HTTP surface: CV upload, profile, matches (§12.2, §12.3)."""

from __future__ import annotations

import io
import json
import uuid

import pytest
import sqlalchemy as sa
from docx import Document as DocxDocument
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.documents import DOCX_MIME
from app.main import app
from app.services.matching import MODEL_VERSION

pytestmark = pytest.mark.db


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def account(client: TestClient, db_session: Session) -> tuple[uuid.UUID, dict[str, str]]:
    """A registered user and the Authorization header that authenticates them.

    Registration goes through the real endpoint rather than an INSERT: the
    fixture then exercises the same path a user takes, including consent.
    """
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:8]}@example.com",
            "password": "a-long-enough-passphrase",
            "accept_processing": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture
def user_id(account: tuple[uuid.UUID, dict[str, str]]) -> uuid.UUID:
    return account[0]


@pytest.fixture
def auth(account: tuple[uuid.UUID, dict[str, str]]) -> dict[str, str]:
    return account[1]


def cv_upload() -> tuple[str, bytes, str]:
    document = DocxDocument()
    for line in [
        "Ahmed Metawea",
        "Senior Data Engineer",
        "ahmed@example.com | +20 100 123 4567",
        "",
        "Summary",
        "Data engineer with five years building streaming and batch platforms for mobile "
        "analytics and telecom reporting, owning pipelines end to end.",
        "",
        "Experience",
        "• Data Engineer, Instabug (2021-2024): built streaming pipelines in Python and "
        "Kafka processing four million events per day.",
        "• Analyst, Vodafone Egypt (2019-2021): SQL reporting and dashboards.",
        "",
        "Education",
        "BSc Computer Engineering, Cairo University (2019).",
        "",
        "Skills",
        "Python, SQL, Apache Kafka, PostgreSQL, Docker",
    ]:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return ("cv.docx", buffer.getvalue(), DOCX_MIME)


def test_upload_requires_a_user(client: TestClient) -> None:
    response = client.post("/api/v1/cv", files={"file": cv_upload()})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_forged_token_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cv", files={"file": cv_upload()}, headers={"Authorization": "Bearer not-a-token"}
    )
    assert response.status_code == 401


def test_upload_returns_the_parseability_report(
    client: TestClient, auth: dict[str, str], db_session: Session
) -> None:
    response = client.post("/api/v1/cv", files={"file": cv_upload()}, headers=auth)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["parse_quality"] > 0.6
    assert body["parseability"]["is_processable"]
    assert "experience" in body["parseability"]["sections_found"]


def test_unsupported_type_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/cv",
        files={"file": ("cv.exe", b"MZ\x00binary", "application/x-msdownload")},
        headers=auth,
    )
    assert response.status_code == 415


def test_unreadable_cv_returns_the_report_not_a_bare_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A candidate whose CV cannot be read deserves something to act on."""
    document = DocxDocument()
    document.add_paragraph("Ahmed Metawea")
    buffer = io.BytesIO()
    document.save(buffer)

    response = client.post(
        "/api/v1/cv",
        files={"file": ("cv.docx", buffer.getvalue(), DOCX_MIME)},
        headers=auth,
    )
    assert response.status_code == 422
    body = response.json()
    assert response.headers["content-type"].startswith("application/problem+json")
    # The report is carried as problem-document members, not stringified into
    # the title, so a client can render it.
    assert body["parseability"]["findings"]
    assert all(finding["suggestion"] for finding in body["parseability"]["findings"])


def test_profile_is_404_before_a_cv_is_processed(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/api/v1/profile", headers=auth)
    assert response.status_code == 404


def test_profile_exposes_evidence_spans(
    client: TestClient, db_session: Session, user_id: uuid.UUID, auth: dict[str, str]
) -> None:
    """§12.2: every field carries the offsets that justify it."""
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :sha) RETURNING id"
        ),
        {"u": user_id, "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    cv_version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'Built pipelines in Python', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id, years_experience, "
            "seniority_level, extraction_confidence) "
            "VALUES (:u, :v, 5, 'senior', 0.9) RETURNING id"
        ),
        {"u": user_id, "v": cv_version_id},
    ).scalar_one()
    skill_id = db_session.execute(
        sa.text(
            "INSERT INTO skills (canonical_name, kind) VALUES ('Python', 'language') "
            "ON CONFLICT (canonical_name) DO UPDATE SET kind = 'language' RETURNING id"
        )
    ).scalar_one()
    db_session.execute(
        sa.text(
            "INSERT INTO profile_skills (profile_id, skill_id, evidence_span, source) "
            "VALUES (:p, :s, '[20,26)'::int4range, 'extracted')"
        ),
        {"p": profile_id, "s": skill_id},
    )
    db_session.commit()

    body = client.get("/api/v1/profile", headers=auth).json()
    assert body["years_experience"] == 5.0
    assert body["skills"][0]["name"] == "Python"
    assert body["skills"][0]["evidence_span"] == [20, 26]


def test_matches_refresh_needs_a_profile(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/api/v1/matches/refresh", headers=auth)
    assert response.status_code == 404


def test_match_list_is_percentile_presented(
    client: TestClient, db_session: Session, user_id: uuid.UUID, auth: dict[str, str]
) -> None:
    """§8.5 and ADR 0006: percentile, never a probability of an outcome."""
    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) VALUES ('greenhouse', :n, '{}'::jsonb) "
            "RETURNING id"
        ),
        {"n": f"greenhouse:{uuid.uuid4().hex[:6]}"},
    ).scalar_one()
    posting_id = db_session.execute(
        sa.text(
            """
            INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                description_text, apply_url, source_url, canonical_url, url_status, status)
            VALUES (:s, '1', 'Data Engineer', 'data engineer', 'body',
                    'https://boards.greenhouse.io/acme/jobs/1?gh_jid=42',
                    'https://boards.greenhouse.io/acme/jobs/1',
                    'https://boards.greenhouse.io/acme/jobs/1', 'live', 'open')
            RETURNING id
            """
        ),
        {"s": source_id},
    ).scalar_one()
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :sha) RETURNING id"
        ),
        {"u": user_id, "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    cv_version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'cv', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id) VALUES (:u, :v) RETURNING id"
        ),
        {"u": user_id, "v": cv_version_id},
    ).scalar_one()
    db_session.execute(
        sa.text(
            """
            INSERT INTO matches (user_id, profile_id, posting_id, total_score, percentile,
                                 gate_passed, subscores, rank, model_version, explanation, gaps)
            VALUES (:u, :p, :j, 0.72, 96.0, true, CAST(:subscores AS jsonb), 1, :version,
                    'Matches 3/5 of the listed skill requirements.', ARRAY['Kubernetes'])
            """
        ),
        {
            "u": user_id,
            "p": profile_id,
            "j": posting_id,
            "subscores": json.dumps({"skill_coverage": 0.6, "contributions": {}}),
            "version": MODEL_VERSION,
        },
    )
    db_session.commit()

    body = client.get("/api/v1/matches", headers=auth).json()
    card = body["matches"][0]
    assert card["presentation"]["band"] == "top 5%"
    assert "roles reviewed for you" in card["presentation"]["summary"]
    assert "total_score" not in card, "the absolute score is not presented"
    assert card["gaps"] == ["Kubernetes"]
    # §12.7: the apply URL is returned exactly as stored.
    assert card["apply_url"] == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=42"

    detail = client.get(f"/api/v1/matches/{card['match_id']}", headers=auth).json()
    assert detail["apply_url"] == card["apply_url"]
    assert detail["url_status"] == "live"
    assert "subscores" in detail


def test_withheld_explains_each_reason(
    client: TestClient, db_session: Session, user_id: uuid.UUID, auth: dict[str, str]
) -> None:
    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) VALUES ('greenhouse', :n, '{}'::jsonb) "
            "RETURNING id"
        ),
        {"n": f"greenhouse:{uuid.uuid4().hex[:6]}"},
    ).scalar_one()
    posting_id = db_session.execute(
        sa.text(
            "INSERT INTO job_postings (source_id, external_id, title, title_normalized, "
            "description_text, apply_url, source_url) "
            "VALUES (:s, '1', 'Engineer', 'engineer', 'body', 'https://x/1', 'https://x/1') "
            "RETURNING id"
        ),
        {"s": source_id},
    ).scalar_one()
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :sha) RETURNING id"
        ),
        {"u": user_id, "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    cv_version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'cv', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id) VALUES (:u, :v) RETURNING id"
        ),
        {"u": user_id, "v": cv_version_id},
    ).scalar_one()
    db_session.execute(
        sa.text(
            """
            INSERT INTO matches (user_id, profile_id, posting_id, total_score, gate_passed,
                                 gate_failures, subscores, model_version)
            VALUES (:u, :p, :j, 0, false, ARRAY['work_authorization','location'],
                    '{}'::jsonb, :version)
            """
        ),
        {"u": user_id, "p": profile_id, "j": posting_id, "version": MODEL_VERSION},
    )
    db_session.commit()

    body = client.get("/api/v1/matches/withheld", headers=auth).json()
    assert body["summary"] == {"work_authorization": 1, "location": 1}
    reasons = body["withheld"][0]["reasons"]
    # Withholding without an explanation is indistinguishable from a broken search.
    assert all(len(reason["explanation"]) > 20 for reason in reasons)
