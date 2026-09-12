"""Per-user preference learning, end to end (§11.8, §18 Phase 6).

The unit suite proves the model fits and the bound holds. What matters here is
the gate around it: that a fit which does not demonstrably help this user is
stored and *not* applied, that the ranking only changes when it does help, and
that the candidate can see and undo it either way.
"""

from __future__ import annotations

import json
import random
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.domain.scoring.aggregate import ScoreWeights
from app.domain.scoring.preferences import FEATURES, MIN_EVENTS, WEIGHT_BOUND
from app.main import app
from app.services.personalisation import MIN_IMPROVEMENT, PersonalisationService

pytestmark = pytest.mark.db

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def account(client: TestClient) -> tuple[uuid.UUID, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:10]}@example.com",
            "password": PASSWORD,
            "accept_processing": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture
def profile(db_session: Session, account: tuple[uuid.UUID, dict[str, str]]) -> uuid.UUID:
    user_id, _ = account
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :s) RETURNING id"
        ),
        {"u": user_id, "s": uuid.uuid4().hex * 2},
    ).scalar_one()
    version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'Built pipelines in Python', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id) VALUES (:u, :v) RETURNING id"
        ),
        {"u": user_id, "v": version_id},
    ).scalar_one()
    db_session.commit()
    return uuid.UUID(str(profile_id))


def _source(db_session: Session) -> uuid.UUID:
    return db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', :n, '{}'::jsonb) RETURNING id"
        ),
        {"n": f"greenhouse:{uuid.uuid4().hex[:8]}"},
    ).scalar_one()


def seed_engagement(
    db_session: Session,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    *,
    count: int,
    driver: str,
    label_pairs: bool = True,
    seed: int = 13,
) -> list[uuid.UUID]:
    """A user whose saves and dismissals are decided by one sub-score.

    Each posting gets a match row carrying the sub-scores the user was shown,
    an engagement event, and — when `label_pairs` — a grade consistent with the
    same driver, which is what the validation step scores against.
    """
    rng = random.Random(seed)
    source_id = _source(db_session)
    posting_ids: list[uuid.UUID] = []

    for index in range(count):
        posting_id = db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                    description_text, apply_url, source_url, status)
                VALUES (:s, :e, 'Data Engineer', 'data engineer', 'body', :url, :url, 'open')
                RETURNING id
                """
            ),
            {"s": source_id, "e": f"{uuid.uuid4().hex}", "url": f"https://x.test/{index}"},
        ).scalar_one()
        posting_ids.append(posting_id)

        subscores = {name: round(rng.random(), 4) for name in FEATURES}
        likes = subscores[driver] > 0.5

        db_session.execute(
            sa.text(
                """
                INSERT INTO matches (user_id, profile_id, posting_id, total_score, gate_passed,
                    subscores, model_version, rank)
                VALUES (:u, :p, :j, :total, true, CAST(:sub AS jsonb), 'test', :rank)
                """
            ),
            {
                "u": user_id,
                "p": profile_id,
                "j": posting_id,
                "total": round(sum(subscores.values()) / len(FEATURES), 4),
                "sub": json.dumps(subscores),
                "rank": index % 25 + 1,
            },
        )
        db_session.execute(
            sa.text("INSERT INTO user_job_events (user_id, posting_id, event) VALUES (:u, :j, :e)"),
            {"u": user_id, "j": posting_id, "e": "saved" if likes else "dismissed"},
        )
        if label_pairs:
            db_session.execute(
                sa.text(
                    "INSERT INTO eval_labels (profile_id, posting_id, grade, labeler) "
                    "VALUES (:p, :j, :g, 'operator')"
                ),
                {"p": profile_id, "j": posting_id, "g": 3 if likes else 0},
            )

    db_session.commit()
    return posting_ids


# ── The activation gate ───────────────────────────────────────────────


def test_below_the_event_gate_nothing_is_fitted(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    """§11.8 activates at 200 events. Below it, there is no model to explain."""
    user_id, _ = account
    seed_engagement(db_session, user_id, profile, count=40, driver="freshness")

    outcome = PersonalisationService(db_session).train(user_id)

    assert not outcome.applied
    assert str(MIN_EVENTS) in outcome.reason
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM user_score_weights WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        == 0
    ), "a refusal below the gate must not write a row"


def test_views_do_not_count_toward_the_gate(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    """A view is what the ranking showed, not what the candidate chose."""
    user_id, _ = account
    posting_ids = seed_engagement(db_session, user_id, profile, count=40, driver="freshness")
    for posting_id in posting_ids:
        db_session.execute(
            sa.text(
                "INSERT INTO user_job_events (user_id, posting_id, event) VALUES (:u, :j, 'viewed')"
            ),
            {"u": user_id, "j": posting_id},
        )
    db_session.commit()

    outcome = PersonalisationService(db_session).train(user_id)
    assert outcome.events == 40, "200 views must not unlock personalisation"


# ── The validation gate ───────────────────────────────────────────────


def test_a_fit_that_helps_is_applied_and_bounded(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    """The whole point of Phase 6: weights move, but only on evidence."""
    user_id, _ = account
    seed_engagement(db_session, user_id, profile, count=260, driver="freshness")

    service = PersonalisationService(db_session)
    outcome = service.train(user_id)

    assert outcome.applied, outcome.reason
    assert outcome.improvement is not None and outcome.improvement >= MIN_IMPROVEMENT
    assert outcome.report is not None
    assert outcome.report.coefficients.strongest() == "freshness"

    weights = service.weights_for(user_id)
    assert weights is not None
    assert weights.total() == pytest.approx(1.0, abs=1e-9)

    defaults = ScoreWeights()
    for name in FEATURES:
        ratio = getattr(weights, name) / getattr(defaults, name)
        assert 1 - WEIGHT_BOUND - 1e-9 <= ratio <= 1 + WEIGHT_BOUND + 1e-9
    assert weights.freshness > defaults.freshness


def test_a_fit_with_no_labels_to_check_against_is_held_not_applied(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    """§11.8 validates against the user's own labels. No labels, no application."""
    user_id, _ = account
    seed_engagement(
        db_session, user_id, profile, count=260, driver="skill_coverage", label_pairs=False
    )

    service = PersonalisationService(db_session)
    outcome = service.train(user_id)

    assert not outcome.applied
    assert "labelled pairs" in outcome.reason
    assert service.weights_for(user_id) is None, "an unvalidated fit must not rank anything"

    # …but the attempt is recorded, so the next run knows it was tried.
    stored = service.explain(user_id)
    assert stored is not None and stored["active"] is False
    assert stored["rejected_reason"]


def test_a_fit_that_does_not_beat_the_defaults_is_rejected(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    """Engagement that contradicts the grades must not be allowed to reweight.

    This is the case the gate exists for: the events say one thing, the user's
    own graded matches say another, and the defaults win.
    """
    user_id, _ = account
    rng = random.Random(29)
    source_id = _source(db_session)

    for index in range(260):
        posting_id = db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                    description_text, apply_url, source_url, status)
                VALUES (:s, :e, 'Data Engineer', 'data engineer', 'b', :url, :url, 'open')
                RETURNING id
                """
            ),
            {"s": source_id, "e": uuid.uuid4().hex, "url": f"https://y.test/{index}"},
        ).scalar_one()
        subscores = {name: round(rng.random(), 4) for name in FEATURES}
        # Freshness is the mirror of skill coverage, so a weighting that chases
        # freshness actively demotes the postings the grades call good.
        subscores["freshness"] = round(1.0 - subscores["skill_coverage"], 4)
        db_session.execute(
            sa.text(
                """
                INSERT INTO matches (user_id, profile_id, posting_id, total_score, gate_passed,
                    subscores, model_version, rank)
                VALUES (:u, :p, :j, 0.5, true, CAST(:sub AS jsonb), 'test', :rank)
                """
            ),
            {
                "u": user_id,
                "p": profile,
                "j": posting_id,
                "sub": json.dumps(subscores),
                "rank": index % 25 + 1,
            },
        )
        # Events driven by freshness…
        db_session.execute(
            sa.text("INSERT INTO user_job_events (user_id, posting_id, event) VALUES (:u,:j,:e)"),
            {
                "u": user_id,
                "j": posting_id,
                "e": "saved" if subscores["freshness"] > 0.5 else "dismissed",
            },
        )
        # …but the grades say skill coverage is what actually mattered.
        db_session.execute(
            sa.text(
                "INSERT INTO eval_labels (profile_id, posting_id, grade, labeler) "
                "VALUES (:p, :j, :g, 'operator')"
            ),
            # Grade 3 is deliberately rare: with a third of the corpus graded 3,
            # any weighting fills the top ten with them and NDCG@10 saturates at
            # 1.0 — the gate would look like it passed without being tested.
            {"p": profile, "j": posting_id, "g": 3 if subscores["skill_coverage"] > 0.93 else 0},
        )
    db_session.commit()

    service = PersonalisationService(db_session)
    outcome = service.train(user_id)

    assert not outcome.applied, "a fit must beat the defaults on the user's own labels"
    assert outcome.ndcg_default is not None
    assert service.weights_for(user_id) is None


def test_retraining_replaces_rather_than_accumulates(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]], profile: uuid.UUID
) -> None:
    user_id, _ = account
    seed_engagement(db_session, user_id, profile, count=260, driver="freshness")

    service = PersonalisationService(db_session)
    service.train(user_id)
    service.train(user_id)

    rows = db_session.execute(
        sa.text("SELECT count(*) FROM user_score_weights WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
    assert rows == 1, "one row per user, replaced in place"


# ── Visibility and control ────────────────────────────────────────────


def test_the_candidate_can_see_why_their_list_is_weighted_as_it_is(
    client: TestClient,
    db_session: Session,
    account: tuple[uuid.UUID, dict[str, str]],
    profile: uuid.UUID,
) -> None:
    """§11.8's interpretability constraint is only worth anything if it is shown."""
    user_id, headers = account
    seed_engagement(db_session, user_id, profile, count=260, driver="freshness")
    PersonalisationService(db_session).train(user_id)
    db_session.commit()

    response = client.get("/api/v1/me/personalisation", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["active"] is True
    assert set(body["weights"]) == set(FEATURES)
    assert body["adjustments"]["freshness"] > 0
    assert body["ndcg_personalised"] > body["ndcg_default"]
    assert "40%" in body["explanation"]


def test_an_unpersonalised_account_is_told_what_would_change_that(
    client: TestClient, account: tuple[uuid.UUID, dict[str, str]]
) -> None:
    _, headers = account
    body = client.get("/api/v1/me/personalisation", headers=headers).json()

    assert body["active"] is False
    assert body["events"] == 0
    assert body["events_required"] == MIN_EVENTS
    assert "standard weighting" in body["explanation"]


def test_personalisation_can_be_turned_off(
    client: TestClient,
    db_session: Session,
    account: tuple[uuid.UUID, dict[str, str]],
    profile: uuid.UUID,
) -> None:
    """A ranking that changed for reasons the candidate did not choose needs an
    off switch."""
    user_id, headers = account
    seed_engagement(db_session, user_id, profile, count=260, driver="freshness")
    PersonalisationService(db_session).train(user_id)
    db_session.commit()
    assert PersonalisationService(db_session).weights_for(user_id) is not None

    response = client.delete("/api/v1/me/personalisation", headers=headers)
    assert response.status_code == 200
    assert response.json()["changed"] is True

    db_session.expire_all()
    assert PersonalisationService(db_session).weights_for(user_id) is None
    assert client.delete("/api/v1/me/personalisation", headers=headers).json()["changed"] is (False)


def test_personalisation_is_scoped_to_the_account(
    client: TestClient,
    db_session: Session,
    account: tuple[uuid.UUID, dict[str, str]],
    profile: uuid.UUID,
) -> None:
    user_id, _ = account
    seed_engagement(db_session, user_id, profile, count=260, driver="freshness")
    PersonalisationService(db_session).train(user_id)
    db_session.commit()

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:10]}@example.com",
            "password": PASSWORD,
            "accept_processing": True,
        },
    ).json()
    body = client.get(
        "/api/v1/me/personalisation",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    ).json()

    assert body["active"] is False
    assert body["events"] == 0
