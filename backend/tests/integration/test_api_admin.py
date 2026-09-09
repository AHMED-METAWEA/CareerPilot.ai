"""Admin API surface (§12.6, §12.7)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app

pytestmark = pytest.mark.db

AUTH = {"X-Admin-Token": "local-admin-token"}


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app import config as config_module

    monkeypatch.setenv("ADMIN_TOKEN", "local-admin-token")
    config_module.reset_config_cache()
    return TestClient(app)


def test_health_needs_no_auth(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_readiness_reports_the_schema_revision(client: TestClient) -> None:
    body = client.get("/health/ready").json()
    assert body["status"] == "ok" and body["schema_revision"]


def test_admin_requires_a_token(client: TestClient) -> None:
    response = client.get("/api/v1/admin/sources")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert {"type", "title", "status", "request_id"} <= set(body)


def test_wrong_token_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/admin/sources", headers={"X-Admin-Token": "nope"}).status_code == 401


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


def test_sources_endpoint_reports_health(
    client: TestClient, db_session: Session, source_row
) -> None:
    source_id = source_row()
    db_session.execute(
        sa.text(
            """
            INSERT INTO source_runs (source_id, started_at, finished_at, fetched, new_count, status)
            VALUES (:id, now(), now(), 42, 7, 'ok')
            """
        ),
        {"id": source_id},
    )
    db_session.commit()

    body = client.get("/api/v1/admin/sources", headers=AUTH).json()
    assert body["counts"]["enabled"] == 1
    source = body["sources"][0]
    assert source["last_fetched"] == 42 and source["last_status"] == "ok"


def test_manual_run_enqueues_once(client: TestClient, source_row) -> None:
    source_id = source_row()
    first = client.post(f"/api/v1/admin/sources/{source_id}/run", headers=AUTH)
    assert first.status_code == 202 and first.json()["status"] == "queued"

    # Already queued: 409 rather than a second identical run.
    second = client.post(f"/api/v1/admin/sources/{source_id}/run", headers=AUTH)
    assert second.status_code == 409


def test_manual_run_on_unknown_source_is_404(client: TestClient) -> None:
    response = client.post(f"/api/v1/admin/sources/{uuid.uuid4()}/run", headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_company_review_queue_is_exposed(client: TestClient, db_session: Session) -> None:
    db_session.execute(
        sa.text(
            """
            INSERT INTO company_review_queue (observed_name, confidence, matched_alias)
            VALUES ('Vodafone Egypt', 0.67, 'Vodafone')
            """
        )
    )
    db_session.commit()

    body = client.get("/api/v1/admin/companies/review", headers=AUTH).json()
    assert body["count"] == 1
    assert body["pending"][0]["observed_name"] == "Vodafone Egypt"


def test_stats_returns_integers_not_floats(client: TestClient) -> None:
    """Counts rendered as 0.0 read as a broken dashboard."""
    corpus = client.get("/api/v1/admin/stats", headers=AUTH).json()["corpus"]
    assert isinstance(corpus["open_postings"], int)


def test_validation_errors_use_problem_json(client: TestClient) -> None:
    response = client.post("/api/v1/admin/sources/not-a-uuid/run", headers=AUTH)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"]


def test_stats_flags_suspected_false_merges(
    client: TestClient, db_session: Session, source_row
) -> None:
    """One company row carrying many distinct employer names is the signature of
    a false merge, and it is worth one query to make it visible (R5)."""
    source_id = source_row()
    company_id = db_session.execute(
        sa.text("INSERT INTO companies (canonical_name) VALUES ('Adyen') RETURNING id")
    ).scalar_one()
    for i, name in enumerate(["Adyen", "Algolia", "Wolt", "Tide", "Typeform"]):
        db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, company_id, company_name_raw,
                                          title, title_normalized, description_text,
                                          apply_url, source_url)
                VALUES (:source_id, :external_id, :company_id, :name, 'Engineer', 'engineer',
                        'body', 'https://x/1', 'https://x/1')
                """
            ),
            {"source_id": source_id, "external_id": str(i), "company_id": company_id, "name": name},
        )
    db_session.commit()

    corpus = client.get("/api/v1/admin/stats", headers=AUTH).json()["corpus"]
    assert corpus["suspected_false_merges"] == 1
