"""Candidate tooling: gaps, interview prep, and generation under the diff (§18, Phase 4).

The unit suite proves the anti-invention diff catches inventions. What matters
here is that the diff is actually *wired in* — that a provider which invents
cannot get its text to a candidate no matter how plausible the prose is. A guard
nobody calls is decoration.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.llm.base import LLMResult
from app.api.v1.tooling import chat_provider
from app.config import get_config
from app.main import app
from app.services.tooling import GenerationRefused, ToolingError, ToolingService

pytestmark = pytest.mark.db

CV_TEXT = """Ahmed Metawea — Data Engineer

- Built streaming pipelines in Python and Apache Kafka at Instabug, processing
  four million crash events per day.
- Rebuilt the nightly reporting stack on Apache Airflow.

Skills: Python, Apache Kafka, Apache Airflow
"""

POSTING_TEXT = """Senior Data Engineer at Acme Analytics.
We need strong Python, streaming experience with Kafka, and working knowledge
of Kubernetes. You will own the pipeline end to end.
"""


class ScriptedProvider:
    """Returns whatever the test tells it to, in order."""

    name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        self.prompts.append(user)
        content = self.replies.pop(0) if self.replies else self.prompts[-1]
        return LLMResult(content=content, model=model, provider=self.name)


PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def account(client: TestClient) -> tuple[uuid.UUID, dict[str, str]]:
    """A real registered user, so the API tests exercise the same auth path."""
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
def scenario(
    db_session: Session, account: tuple[uuid.UUID, dict[str, str]]
) -> dict[str, uuid.UUID]:
    """One candidate, one posting, one scored match — with evidence attached.

    Built through SQL rather than the services above it so that a failure here
    is a failure of the tooling, not of matching.
    """
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
            "parser_version) VALUES (:d, 1, :t, 0.95, 'test') RETURNING id"
        ),
        {"d": document_id, "t": CV_TEXT},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id, years_experience, "
            "seniority_level) VALUES (:u, :v, 5, 'senior') RETURNING id"
        ),
        {"u": user_id, "v": version_id},
    ).scalar_one()

    for ordinal, bullet in enumerate(
        [
            "Built streaming pipelines in Python and Apache Kafka at Instabug, "
            "processing four million crash events per day.",
            "Rebuilt the nightly reporting stack on Apache Airflow.",
        ]
    ):
        db_session.execute(
            sa.text(
                "INSERT INTO profile_bullets (profile_id, ordinal, text, span) "
                "VALUES (:p, :o, :t, int4range(0, 10))"
            ),
            {"p": profile_id, "o": ordinal, "t": bullet},
        )

    skills: dict[str, uuid.UUID] = {}
    for name, kind in (
        ("Python", "language"),
        ("Apache Kafka", "tool"),
        ("Apache Airflow", "tool"),
        ("Kubernetes", "tool"),
    ):
        skills[name] = db_session.execute(
            sa.text(
                "INSERT INTO skills (canonical_name, kind) VALUES (:n, :k) "
                "ON CONFLICT (canonical_name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id"
            ),
            {"n": name, "k": kind},
        ).scalar_one()

    for name in ("Python", "Apache Kafka", "Apache Airflow"):
        db_session.execute(
            sa.text(
                "INSERT INTO profile_skills (profile_id, skill_id, evidence_span, source) "
                "VALUES (:p, :s, int4range(0, 10), 'cv')"
            ),
            {"p": profile_id, "s": skills[name]},
        )

    company_id = db_session.execute(
        sa.text("INSERT INTO companies (canonical_name) VALUES ('Acme Analytics') RETURNING id")
    ).scalar_one()
    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', :n, '{}'::jsonb) RETURNING id"
        ),
        {"n": f"greenhouse:acme:{uuid.uuid4().hex[:6]}"},
    ).scalar_one()
    posting_id = db_session.execute(
        sa.text(
            """
            INSERT INTO job_postings (source_id, external_id, company_id, title,
                title_normalized, description_text, apply_url, source_url, status)
            VALUES (:s, :e, :c, 'Senior Data Engineer', 'senior data engineer', :body,
                    'https://boards.example.com/acme/1', 'https://boards.example.com/acme/1',
                    'open')
            RETURNING id
            """
        ),
        {"s": source_id, "e": uuid.uuid4().hex, "c": company_id, "body": POSTING_TEXT},
    ).scalar_one()

    requirements: dict[str, uuid.UUID] = {}
    for text_, kind, must, skill in (
        ("Strong Python", "skill", True, "Python"),
        ("Streaming experience with Kafka", "skill", True, "Apache Kafka"),
        ("Working knowledge of Kubernetes", "skill", True, "Kubernetes"),
        ("Five years in data engineering", "experience", False, None),
    ):
        requirements[text_] = db_session.execute(
            sa.text(
                "INSERT INTO job_requirements (posting_id, text, kind, is_must_have, skill_id, "
                "span) VALUES (:p, :t, :k, :m, :s, int4range(0, 10)) RETURNING id"
            ),
            {
                "p": posting_id,
                "t": text_,
                "k": kind,
                "m": must,
                "s": skills.get(skill) if skill else None,
            },
        ).scalar_one()

    match_id = db_session.execute(
        sa.text(
            """
            INSERT INTO matches (user_id, profile_id, posting_id, total_score, gate_passed,
                subscores, model_version, gaps)
            VALUES (:u, :p, :j, 0.71, true, '{"skill_coverage": 0.67}'::jsonb, 'test',
                    ARRAY['Kubernetes'])
            RETURNING id
            """
        ),
        {"u": user_id, "p": profile_id, "j": posting_id},
    ).scalar_one()

    db_session.execute(
        sa.text(
            "INSERT INTO match_evidence (match_id, requirement_id, status, note) "
            "VALUES (:m, :r, 'met', :n)"
        ),
        {
            "m": match_id,
            "r": requirements["Strong Python"],
            "n": "Built streaming pipelines in Python and Apache Kafka at Instabug.",
        },
    )
    db_session.execute(
        sa.text(
            "INSERT INTO match_evidence (match_id, requirement_id, status, note) "
            "VALUES (:m, :r, 'not_met', NULL)"
        ),
        {"m": match_id, "r": requirements["Working knowledge of Kubernetes"]},
    )
    db_session.commit()

    return {
        "user_id": user_id,
        "profile_id": profile_id,
        "posting_id": posting_id,
        "match_id": match_id,
    }


def service(session: Session, provider: object | None = None) -> ToolingService:
    return ToolingService(session, get_config(), llm=provider)  # type: ignore[arg-type]


# ── deterministic surfaces ────────────────────────────────────────────


def test_gap_analysis_needs_no_model(db_session: Session, scenario: dict[str, uuid.UUID]) -> None:
    """Gaps come from the scorer's own output, so the report is reproducible."""
    analysis = service(db_session).gap_analysis(scenario["user_id"], scenario["match_id"])

    assert [gap.skill for gap in analysis.gaps] == ["Kubernetes"]
    assert analysis.gaps[0].is_must_have
    assert analysis.gaps[0].suggestion, "a gap with no next step is just bad news"
    assert set(analysis.matched) == {"Python", "Apache Kafka"}


def test_gap_analysis_is_scoped_to_the_owner(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(ToolingError):
        service(db_session).gap_analysis(uuid.uuid4(), scenario["match_id"])


def test_interview_questions_come_from_the_posting(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """Every question traces to a requirement this posting actually stated."""
    prep = service(db_session).interview_preparation(scenario["user_id"], scenario["match_id"])

    requirements = {item["requirement"] for item in prep["questions"]}
    assert requirements == {
        "Strong Python",
        "Streaming experience with Kafka",
        "Working knowledge of Kubernetes",
        "Five years in data engineering",
    }
    assert prep["questions"][0]["must_have"] is True

    by_requirement = {item["requirement"]: item for item in prep["questions"]}
    assert by_requirement["Strong Python"]["your_evidence"]
    assert by_requirement["Working knowledge of Kubernetes"]["your_evidence"] is None
    assert "Decide now" in by_requirement["Working knowledge of Kubernetes"]["advice"]


# ── generation, gated ─────────────────────────────────────────────────


HONEST_LETTER = """I am applying for the Senior Data Engineer role at Acme Analytics.

At Instabug I built streaming pipelines in Python and Apache Kafka, processing
four million crash events per day, and rebuilt the nightly reporting stack on
Apache Airflow.

The posting asks for Kubernetes, which I have not worked with. I would be glad
to talk about how the rest of this experience transfers.
"""

INVENTED_LETTER = """I am applying for the Senior Data Engineer role at Acme Analytics.

My Kubernetes experience would transfer directly to your platform team, and my
work at Google taught me to operate at scale. I improved throughput by 40%.
"""


def test_a_clean_letter_reaches_the_candidate(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    provider = ScriptedProvider(HONEST_LETTER)
    result = service(db_session, provider).cover_letter(scenario["user_id"], scenario["match_id"])

    assert "Instabug" in result["letter"]
    assert len(provider.prompts) == 1, "a clean draft must not be regenerated"


def test_an_inventing_provider_never_reaches_the_candidate(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """The whole point of Phase 4: plausible prose is not enough."""
    provider = ScriptedProvider(INVENTED_LETTER, INVENTED_LETTER)

    with pytest.raises(GenerationRefused) as exc:
        service(db_session, provider).cover_letter(scenario["user_id"], scenario["match_id"])

    kinds = {item.kind.value for item in exc.value.result.invented}
    assert {"skill", "metric"} <= kinds
    assert exc.value.attempts == 2, "one generation, one repair, then an honest refusal"


def test_the_repair_prompt_names_what_was_invented(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """A retry that does not say what was wrong is just another roll of the dice."""
    provider = ScriptedProvider(INVENTED_LETTER, HONEST_LETTER)
    result = service(db_session, provider).cover_letter(scenario["user_id"], scenario["match_id"])

    assert result["letter"].startswith("I am applying")
    assert "Kubernetes" in provider.prompts[1]
    assert "does not support" in provider.prompts[1]


def test_the_fact_bundle_carries_only_facts(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """§10.4: the model receives structured facts, not the raw CV to riff on."""
    provider = ScriptedProvider(HONEST_LETTER)
    service(db_session, provider).cover_letter(scenario["user_id"], scenario["match_id"])

    prompt = provider.prompts[0]
    assert "Apache Airflow" in prompt
    assert "Senior Data Engineer" in prompt
    assert "REQUIREMENTS THEY DO NOT: Kubernetes" in prompt


def test_only_the_candidates_own_bullets_can_be_rewritten(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """Otherwise the endpoint launders a claim the CV never made."""
    provider = ScriptedProvider("Anything at all.")

    with pytest.raises(ToolingError, match="not from your CV"):
        service(db_session, provider).rewrite_bullet(
            scenario["user_id"],
            scenario["match_id"],
            "Led a team of 30 engineers at Google.",
        )
    assert provider.prompts == [], "nothing should reach the model"


def test_a_rewrite_that_adds_a_number_is_discarded(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    original = (
        "Built streaming pipelines in Python and Apache Kafka at Instabug, "
        "processing four million crash events per day."
    )
    inflated = (
        "Built streaming pipelines in Python and Apache Kafka at Instabug, cutting "
        "latency by 60% across 12 services."
    )
    provider = ScriptedProvider(inflated, inflated)

    with pytest.raises(GenerationRefused) as exc:
        service(db_session, provider).rewrite_bullet(
            scenario["user_id"], scenario["match_id"], original
        )
    assert any(item.kind.value == "metric" for item in exc.value.result.invented)


def test_writing_without_a_provider_says_so(
    db_session: Session, scenario: dict[str, uuid.UUID]
) -> None:
    """And says which parts still work, rather than failing opaquely."""
    with pytest.raises(ToolingError, match="Gap analysis and interview"):
        service(db_session).cover_letter(scenario["user_id"], scenario["match_id"])


# ── the API surface ───────────────────────────────────────────────────


def test_the_deterministic_endpoints_need_no_provider(
    client: TestClient,
    account: tuple[uuid.UUID, dict[str, str]],
    scenario: dict[str, uuid.UUID],
) -> None:
    """Gaps and interview prep are available whether or not a model is."""
    _, headers = account
    match_id = scenario["match_id"]

    gaps = client.get(f"/api/v1/matches/{match_id}/gaps", headers=headers)
    assert gaps.status_code == 200, gaps.text
    assert gaps.json()["blocking_count"] == 1
    assert gaps.json()["gaps"][0]["skill"] == "Kubernetes"

    prep = client.get(f"/api/v1/matches/{match_id}/interview", headers=headers)
    assert prep.status_code == 200, prep.text
    assert prep.json()["role"] == "Senior Data Engineer"
    assert len(prep.json()["questions"]) == 4


def test_another_users_match_is_not_readable(
    client: TestClient,
    scenario: dict[str, uuid.UUID],
) -> None:
    """Scoped by user id at the query, not by the route (§16.2)."""
    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:10]}@example.com",
            "password": PASSWORD,
            "accept_processing": True,
        },
    ).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}

    response = client.get(f"/api/v1/matches/{scenario['match_id']}/gaps", headers=headers)
    assert response.status_code == 404


def test_the_tooling_endpoints_require_a_token(
    client: TestClient, scenario: dict[str, uuid.UUID]
) -> None:
    response = client.get(f"/api/v1/matches/{scenario['match_id']}/gaps")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_refusal_is_a_422_that_names_the_invented_claims(
    client: TestClient,
    account: tuple[uuid.UUID, dict[str, str]],
    scenario: dict[str, uuid.UUID],
) -> None:
    """The candidate is told why they are not being shown a letter (§12.7)."""
    _, headers = account
    app.dependency_overrides[chat_provider] = lambda: ScriptedProvider(
        INVENTED_LETTER, INVENTED_LETTER
    )
    try:
        response = client.post(
            f"/api/v1/matches/{scenario['match_id']}/cover-letter", headers=headers
        )
    finally:
        app.dependency_overrides.pop(chat_provider, None)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert "does not support" in body["title"]
    assert any("Kubernetes" in item for item in body["invented"])


def test_a_clean_draft_is_returned_with_its_disclosure(
    client: TestClient,
    account: tuple[uuid.UUID, dict[str, str]],
    scenario: dict[str, uuid.UUID],
) -> None:
    """§14.1: generated text is labelled as generated, every time."""
    _, headers = account
    app.dependency_overrides[chat_provider] = lambda: ScriptedProvider(HONEST_LETTER)
    try:
        response = client.post(
            f"/api/v1/matches/{scenario['match_id']}/cover-letter", headers=headers
        )
    finally:
        app.dependency_overrides.pop(chat_provider, None)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["checked"] is True
    assert "language model" in body["disclosure"]


def test_drafting_without_a_provider_is_a_503_not_a_bad_draft(
    client: TestClient,
    account: tuple[uuid.UUID, dict[str, str]],
    scenario: dict[str, uuid.UUID],
) -> None:
    _, headers = account
    app.dependency_overrides[chat_provider] = lambda: None
    try:
        response = client.post(
            f"/api/v1/matches/{scenario['match_id']}/cover-letter", headers=headers
        )
    finally:
        app.dependency_overrides.pop(chat_provider, None)

    assert response.status_code == 503
    assert "Gap analysis and interview" in response.json()["title"]
