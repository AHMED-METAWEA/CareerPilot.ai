"""The evaluation harness and the golden-set workflow (§9.1, §9.3, §9.6)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.embeddings import build_embedder
from app.adapters.embeddings.deterministic import DeterministicReranker
from app.config import get_config
from app.eval.golden import KAPPA_GATE, agreement, load_labels, record_label, sample_for_labelling
from app.eval.harness import ABLATION_CONFIGURATIONS, EvaluationHarness, check_regression
from app.services.matching import PipelineSettings

pytestmark = pytest.mark.db


@pytest.fixture
def profile_and_postings(db_session: Session) -> tuple[uuid.UUID, list[uuid.UUID]]:
    user_id = db_session.execute(
        sa.text("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id"),
        {"e": f"{uuid.uuid4().hex[:8]}@example.com"},
    ).scalar_one()
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :s) RETURNING id"
        ),
        {"u": user_id, "s": uuid.uuid4().hex * 2},
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
            "INSERT INTO candidate_profiles (user_id, cv_version_id, seniority_level) "
            "VALUES (:u, :v, 'mid') RETURNING id"
        ),
        {"u": user_id, "v": cv_version_id},
    ).scalar_one()

    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', :n, '{}'::jsonb) RETURNING id"
        ),
        {"n": f"greenhouse:{uuid.uuid4().hex[:6]}"},
    ).scalar_one()
    postings = [
        db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                    description_text, apply_url, source_url, canonical_url, posted_at,
                    url_status, last_verified_at, status)
                VALUES (:s, :e, :t, lower(:t), :body, :url, :url, :url, now(), 'live', now(),
                        'open')
                RETURNING id
                """
            ),
            {
                "s": source_id,
                "e": str(index),
                "t": f"Data Engineer {index}",
                "body": "Requirements\n• 3+ years of Python\n• Experience with SQL\n",
                "url": f"https://boards.greenhouse.io/acme/jobs/{index}",
            },
        ).scalar_one()
        for index in range(6)
    ]
    db_session.commit()
    return profile_id, postings


def test_rubric_and_gate_are_stated() -> None:
    from app.eval.golden import RUBRIC

    assert "0 —" in RUBRIC and "3 —" in RUBRIC
    assert KAPPA_GATE == 0.60


def test_labels_round_trip_and_take_the_median(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """Median across annotators: grades are ordinal, and one dissenter should
    not move a pair by half a grade."""
    profile_id, postings = profile_and_postings
    for labeller, grade in (("amir", 3), ("salma", 3), ("karim", 1)):
        record_label(
            db_session,
            profile_id=profile_id,
            posting_id=postings[0],
            grade=grade,
            labeller=labeller,
        )
    db_session.commit()

    labels = load_labels(db_session)
    assert labels[str(profile_id)][str(postings[0])] == 3


def test_grades_outside_the_rubric_are_refused(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    profile_id, postings = profile_and_postings
    with pytest.raises(ValueError, match="grade must be 0-3"):
        record_label(
            db_session, profile_id=profile_id, posting_id=postings[0], grade=5, labeller="x"
        )


def test_agreement_gate(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """§9.1: below κ 0.60 the rubric is defective and no metric is trusted."""
    profile_id, postings = profile_and_postings
    for index, posting_id in enumerate(postings):
        record_label(
            db_session,
            profile_id=profile_id,
            posting_id=posting_id,
            grade=index % 4,
            labeller="amir",
        )
        record_label(
            db_session,
            profile_id=profile_id,
            posting_id=posting_id,
            grade=index % 4,
            labeller="salma",
        )
    db_session.commit()

    stats = agreement(db_session)
    assert stats.labellers == 2
    assert stats.cohens_kappa == 1.0
    assert stats.passes_gate

    # Now add a third annotator who disagrees with everyone.
    for index, posting_id in enumerate(postings):
        record_label(
            db_session,
            profile_id=profile_id,
            posting_id=posting_id,
            grade=(index + 2) % 4,
            labeller="karim",
        )
    db_session.commit()
    assert agreement(db_session).fleiss_kappa is not None


def test_sampling_is_stratified_across_deciles(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """Labelling only the top of the ranking measures how good the system is at
    cases it already likes."""
    profile_id, postings = profile_and_postings
    user_id = db_session.execute(
        sa.text("SELECT user_id FROM candidate_profiles WHERE id = :id"), {"id": profile_id}
    ).scalar_one()
    for index, posting_id in enumerate(postings):
        db_session.execute(
            sa.text(
                """
                INSERT INTO matches (user_id, profile_id, posting_id, total_score, gate_passed,
                                     subscores, model_version)
                VALUES (:u, :p, :j, :score, true, '{}'::jsonb, 'match-1')
                """
            ),
            {"u": user_id, "p": profile_id, "j": posting_id, "score": 0.9 - index * 0.1},
        )
    db_session.commit()

    sample = sample_for_labelling(db_session, profile_id, per_decile=1)
    assert sample
    assert len({row["decile"] for row in sample}) > 1, "the sample must span deciles"


def test_harness_reports_nothing_without_labels(db_session: Session) -> None:
    """The golden set is human work; the harness says so rather than inventing."""
    harness = EvaluationHarness(db_session, get_config(), embedder=build_embedder("deterministic"))
    report = harness.run()
    assert report.results == []
    assert report.labelled_pairs == 0


def test_ablation_runs_every_configuration(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """The §9.3 table: four configurations of the pipeline that ships."""
    profile_id, postings = profile_and_postings
    for index, posting_id in enumerate(postings):
        record_label(
            db_session,
            profile_id=profile_id,
            posting_id=posting_id,
            grade=3 if index < 2 else 0,
            labeller="amir",
        )
    db_session.commit()

    from app.services.embedding import EmbeddingService

    embedder = build_embedder("deterministic")
    EmbeddingService(db_session, embedder).embed_pending(batch_size=50)
    db_session.commit()

    harness = EvaluationHarness(
        db_session, get_config(), embedder=embedder, reranker=DeterministicReranker()
    )
    report = harness.run()

    assert len(report.results) == len(ABLATION_CONFIGURATIONS)
    assert [result.label for result in report.results] == [
        settings.label for settings in ABLATION_CONFIGURATIONS
    ]
    table = report.markdown_table()
    assert "NDCG@10" in table and "p95 latency" in table
    for result in report.results:
        assert 0.0 <= result.metrics.ndcg_at_10 <= 1.0


def test_evaluation_leaves_no_matches_behind(
    db_session: Session, profile_and_postings: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """Evaluating must not write matches the product would then serve."""
    profile_id, postings = profile_and_postings
    record_label(
        db_session, profile_id=profile_id, posting_id=postings[0], grade=3, labeller="amir"
    )
    db_session.commit()

    harness = EvaluationHarness(db_session, get_config(), embedder=build_embedder("deterministic"))
    harness.run(configurations=[PipelineSettings()])
    db_session.commit()

    assert db_session.execute(sa.text("SELECT count(*) FROM matches")).scalar_one() == 0


def test_regression_gate() -> None:
    """§9.6: points of NDCG, not percent."""
    ok, message = check_regression(0.73, 0.75)
    assert ok and "fell 2.0 points" in message


def test_the_tolerance_boundary_passes() -> None:
    """ "More than two points" means exactly two points passes.

    In binary, 0.75 - 0.73 is 2.0000000000000018, which failed the build for a
    drop the specification allows.
    """
    assert check_regression(0.73, 0.75, tolerance=2.0)[0] is True
    assert check_regression(0.7299, 0.75, tolerance=2.0)[0] is False

    failed, message = check_regression(0.70, 0.75)
    assert not failed and "tolerance" in message

    improved, message = check_regression(0.80, 0.75)
    assert improved and "rose" in message

    first_run, message = check_regression(0.75, None)
    assert first_run and "no previous evaluation" in message
