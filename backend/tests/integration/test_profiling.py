"""Onboarding and profile construction against a real database (§11.1)."""

from __future__ import annotations

import io
import json
import uuid

import pytest
import sqlalchemy as sa
from docx import Document as DocxDocument
from sqlalchemy.orm import Session

from app.adapters.documents import DOCX_MIME, PDF_MIME
from app.adapters.embeddings import build_embedder
from app.adapters.llm.base import LLMResult
from app.config import BASE_DIR, get_config
from app.services.profiling import CvUnreadableError, ProfilingService
from app.services.taxonomy import load_seed, sync_skills

pytestmark = pytest.mark.db

CV_LINES = [
    "Ahmed Metawea",
    "Senior Data Engineer",
    "ahmed.metawea001@gmail.com | +20 100 123 4567 | Cairo, Egypt",
    "",
    "Summary",
    "Data engineer with five years building streaming and batch platforms for mobile "
    "analytics and telecom reporting, comfortable owning a pipeline end to end.",
    "",
    "Experience",
    "• Data Engineer, Instabug (2021-2024): built streaming pipelines in Python and Kafka "
    "processing four million crash events per day, cutting ingestion latency from forty "
    "minutes to under three.",
    "• Rebuilt the nightly reporting stack, orchestrated with Airflow, reducing failed runs "
    "from a dozen a week to fewer than one.",
    "• Analyst, Vodafone Egypt (2019-2021): SQL reporting and dashboards for network "
    "operations across fourteen governorates.",
    "",
    "Education",
    "BSc Computer Engineering, Cairo University (2019).",
    "",
    "Skills",
    "Python, SQL, Apache Kafka, Apache Airflow, PostgreSQL, Docker",
    "",
    "Languages",
    "Arabic (native), English (C1)",
]

EXTRACTION = {
    "years_experience": 5,
    "years_experience_quote": "Data Engineer, Instabug (2021-2024)",
    "seniority_level": "senior",
    "seniority_quote": "Senior Data Engineer",
    "locations": [{"value": "Cairo", "quote": "Cairo, Egypt"}],
    "work_authorization": [],
    "languages": [{"language": "Arabic", "cefr": "native", "quote": "Arabic (native)"}],
    "skills": [
        {"name": "Python", "quote": "Python and Kafka"},
        {"name": "Kafka", "quote": "Python and Kafka"},
        {"name": "Kubernetes", "quote": "ran Kubernetes across three clusters"},
        {"name": "Quantum Computing", "quote": "SQL reporting and dashboards"},
    ],
    "summary": "Data engineer with streaming experience.",
}


class StubProvider:
    """Stands in for Groq. Also asserts the CV arrived redacted."""

    name = "stub"

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload if payload is not None else EXTRACTION
        self.prompts: list[str] = []

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        self.prompts.append(user)
        return LLMResult(
            content=json.dumps(self.payload),
            model=model,
            provider=self.name,
            tokens_in=1200,
            tokens_out=180,
            latency_ms=400.0,
        )


def docx_bytes(lines: list[str]) -> bytes:
    document = DocxDocument()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def user_id(db_session: Session) -> uuid.UUID:
    return db_session.execute(
        sa.text("INSERT INTO users (email, password_hash) VALUES (:email, 'x') RETURNING id"),
        {"email": f"{uuid.uuid4().hex[:10]}@example.com"},
    ).scalar_one()


@pytest.fixture
def service(db_session: Session) -> ProfilingService:
    sync_skills(db_session, load_seed(BASE_DIR / "config" / "skills.yaml"))
    return ProfilingService(
        db_session,
        get_config(),
        llm=StubProvider(),
        embedder=build_embedder("deterministic"),
    )


def test_ingest_stores_document_version_and_quality(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    result = service.ingest_cv(user_id, docx_bytes(CV_LINES), DOCX_MIME)
    assert result.parseability.is_processable
    assert result.version == 1

    row = db_session.execute(
        sa.text("SELECT parse_quality, parser_version, raw_text FROM cv_versions")
    ).one()
    assert float(row.parse_quality) == result.parseability.quality
    assert "Instabug" in row.raw_text


def test_reuploading_the_same_bytes_adds_a_version_not_a_document(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    data = docx_bytes(CV_LINES)
    first = service.ingest_cv(user_id, data, DOCX_MIME)
    second = service.ingest_cv(user_id, data, DOCX_MIME)

    assert first.cv_document_id == second.cv_document_id
    assert second.version == 2
    assert db_session.execute(sa.text("SELECT count(*) FROM cv_documents")).scalar_one() == 1


def test_unreadable_cv_halts_with_a_report(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    """§11.1 step 5: below the floor the pipeline stops and explains itself."""
    with pytest.raises(CvUnreadableError) as exc:
        service.ingest_cv(user_id, docx_bytes(["Ahmed Metawea"]), DOCX_MIME)

    assert not exc.value.report.is_processable
    assert exc.value.report.findings
    # The version is still stored, so the candidate can see the report and we
    # can measure how often this happens.
    assert db_session.execute(sa.text("SELECT count(*) FROM cv_versions")).scalar_one() == 1


def test_cv_is_redacted_before_it_reaches_the_model(
    db_session: Session, user_id: uuid.UUID
) -> None:
    """§16.2: nothing personal leaves the process unredacted."""
    provider = StubProvider()
    service = ProfilingService(
        db_session, get_config(), llm=provider, embedder=build_embedder("deterministic")
    )
    result = service.ingest_cv(user_id, docx_bytes(CV_LINES), DOCX_MIME)
    service.build_profile(user_id, result.cv_version_id)

    prompt = provider.prompts[0]
    assert "ahmed.metawea001@gmail.com" not in prompt
    assert "+20 100 123 4567" not in prompt
    assert "[[EMAIL_1]]" in prompt
    # The professional content still has to be there, or extraction is pointless.
    assert "Instabug" in prompt and "Kafka" in prompt


def test_profile_persists_only_grounded_claims(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    ingest = service.ingest_cv(user_id, docx_bytes(CV_LINES), DOCX_MIME)
    result = service.build_profile(user_id, ingest.cv_version_id)

    skills = set(
        db_session.execute(
            sa.text(
                "SELECT s.canonical_name FROM profile_skills ps "
                "JOIN skills s ON s.id = ps.skill_id WHERE ps.profile_id = :id"
            ),
            {"id": result.profile_id},
        )
        .scalars()
        .all()
    )
    assert skills == {"Python", "Apache Kafka"}
    # Invented: quote is not in the CV.
    assert "skill:Kubernetes" in result.grounded.discarded
    # Real quote, outside the vocabulary: queued, not invented (§10.2).
    assert [match.token for match in result.grounded.unmapped_skills] == ["Quantum Computing"]
    assert (
        db_session.execute(
            sa.text("SELECT occurrences FROM unmapped_skills WHERE token = 'Quantum Computing'")
        ).scalar_one()
        == 1
    )


def test_bullets_are_stored_with_spans_and_vectors(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    ingest = service.ingest_cv(user_id, docx_bytes(CV_LINES), DOCX_MIME)
    result = service.build_profile(user_id, ingest.cv_version_id)

    rows = db_session.execute(
        sa.text(
            "SELECT text, lower(span) AS s, upper(span) AS e, embedding IS NOT NULL AS embedded "
            "FROM profile_bullets WHERE profile_id = :id ORDER BY ordinal"
        ),
        {"id": result.profile_id},
    ).all()
    assert len(rows) == result.bullets > 0
    raw_text = db_session.execute(
        sa.text("SELECT raw_text FROM cv_versions WHERE id = :id"), {"id": ingest.cv_version_id}
    ).scalar_one()
    for row in rows:
        assert row.embedded
        # The span must recover the bullet from the candidate's own CV. Compared
        # on normalised whitespace: a bullet wrapped across lines in the source
        # is stored as one line, and the span still covers both.
        recovered = " ".join(raw_text[row.s : row.e].split())
        assert recovered.startswith(" ".join(row.text.split())[:30])


def test_extraction_run_is_recorded_with_its_metrics(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    """§17.2: every LLM call records tokens, latency and validation outcome."""
    ingest = service.ingest_cv(user_id, docx_bytes(CV_LINES), DOCX_MIME)
    service.build_profile(user_id, ingest.cv_version_id)

    row = db_session.execute(
        sa.text("SELECT component, metrics FROM model_runs ORDER BY run_at DESC LIMIT 1")
    ).one()
    assert row.component == "profile_extraction"
    assert row.metrics["tokens_in"] == 1200
    assert row.metrics["fields_discarded"] >= 1
    assert 0.0 <= row.metrics["hallucination_rate"] <= 1.0


def test_mislabelled_pdf_is_rejected(
    db_session: Session, user_id: uuid.UUID, service: ProfilingService
) -> None:
    from app.adapters.documents import ExtractionFailedError

    with pytest.raises(ExtractionFailedError):
        service.ingest_cv(user_id, b"this is not a pdf", PDF_MIME)
