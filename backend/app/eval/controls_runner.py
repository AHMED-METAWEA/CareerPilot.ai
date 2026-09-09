"""Running the negative controls against the real corpus (§9.4).

The controls themselves are pure (`negative_controls.py`); this assembles real
candidates and postings for them, and builds the scorer they probe.

The scorer used here is the same decomposed one the product uses, minus the
gates — the controls ask whether the *score* measures fit, and gates would mask
that by zeroing whole categories before the score is computed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.embeddings import build_embedder, cosine
from app.adapters.embeddings.base import parse_vector
from app.config import AppConfig
from app.domain.jobs.requirements import expand_skill_requirements, extract_requirements_heuristic
from app.domain.matching.gates import GateResult
from app.domain.models import (
    SENIORITY_ORDER,
    CandidateSnapshot,
    PostingSnapshot,
    Seniority,
)
from app.domain.scoring.aggregate import ScoreWeights, SubScores, aggregate
from app.domain.scoring.subscores import (
    freshness,
    requirement_alignment,
    seniority_fit,
    skill_coverage,
)
from app.eval.negative_controls import (
    ControlResult,
    cross_domain_separation,
    seniority_monotonicity,
    shuffled_pairings,
    summarise,
)
from app.services.taxonomy import load_taxonomy

log = structlog.get_logger(__name__)

OUT_OF_DOMAIN_TITLES = (
    "nurse",
    "nursing",
    "paralegal",
    "attorney",
    "counsel",
    "chef",
    "driver",
    "warehouse",
    "barista",
    "teacher",
)
IN_DOMAIN_TITLES = ("engineer", "developer", "data", "software", "platform", "analytics")


def run_controls(session: Session, config: AppConfig) -> dict[str, Any]:
    """Assemble real data and run every control."""
    embedder = build_embedder(config.models.embedding)
    taxonomy = load_taxonomy(session)

    profiles = (
        session.execute(
            text("SELECT id FROM candidate_profiles ORDER BY extracted_at DESC LIMIT 5")
        )
        .scalars()
        .all()
    )
    if not profiles:
        return summarise([ControlResult("setup", False, "no candidate profiles to test with")])

    candidates = [_load_candidate(session, uuid.UUID(str(pid))) for pid in profiles]
    candidate = candidates[0]

    # The shuffled-pairings control compares true CV-posting pairs against
    # reassigned ones, which is vacuous when every candidate is the same person:
    # the separation comes out at exactly 0.000 and reads as a failure of the
    # scorer rather than of the setup. Synthetic candidates from other
    # disciplines make it meaningful, and §15.3 already contemplates synthetic
    # profiles for exactly this kind of testing.
    synthetic = _synthetic_candidates()
    candidates = candidates + synthetic

    in_domain = _postings_matching(session, IN_DOMAIN_TITLES, limit=20)
    out_of_domain = _postings_matching(session, OUT_OF_DOMAIN_TITLES, limit=20)
    if not out_of_domain:
        # A corpus of engineering boards may genuinely contain no nursing roles.
        # Synthetic postings keep the control meaningful rather than skipping it.
        out_of_domain = _synthetic_out_of_domain()

    # Bullets per candidate, not one candidate's bullets for everyone: with a
    # single shared set, shuffling the pairings changes nothing and the control
    # reports a separation of exactly zero — a bug in the control, not a finding
    # about the scorer.
    bullets_by_profile: dict[str, list[tuple[str, list[float] | None]]] = {
        str(profile_id): _load_bullets(session, uuid.UUID(str(profile_id)))
        for profile_id in profiles
    }
    for candidate_snapshot in synthetic:
        texts = list(candidate_snapshot.bullets)
        bullets_by_profile[candidate_snapshot.profile_id] = list(
            zip(texts, embedder.encode(texts), strict=True)
        )
    scorer = _build_scorer(session, config, embedder, taxonomy, bullets_by_profile)

    results = [
        cross_domain_separation(candidate, in_domain, out_of_domain, scorer),
        seniority_monotonicity(
            candidate, _seniority_ladder(in_domain[0] if in_domain else None), scorer
        ),
        shuffled_pairings(_true_pairs(candidates, in_domain, out_of_domain), scorer),
    ]
    return summarise(results)


def _build_scorer(
    session: Session,
    config: AppConfig,
    embedder: Any,
    taxonomy: Any,
    bullets_by_profile: dict[str, list[tuple[str, list[float] | None]]],
) -> Any:
    weights = ScoreWeights(**config.scoring.weights.model_dump())
    now = datetime.now(UTC)
    descriptions: dict[str, str] = {}

    def description_for(posting: PostingSnapshot) -> str:
        if posting.posting_id not in descriptions:
            row = (
                session.execute(
                    text("SELECT description_text FROM job_postings WHERE id = :id"),
                    {"id": uuid.UUID(posting.posting_id)},
                ).scalar_one_or_none()
                if _is_uuid(posting.posting_id)
                else None
            )
            descriptions[posting.posting_id] = row or posting.title
        return descriptions[posting.posting_id]

    def score(candidate: CandidateSnapshot, posting: PostingSnapshot) -> float:
        bullets = bullets_by_profile.get(candidate.profile_id, [])
        bullet_texts = [text_value for text_value, vector in bullets if vector]
        bullet_vectors = {text_value: vector for text_value, vector in bullets if vector}
        body = description_for(posting)
        requirements = [requirement for requirement, _ in extract_requirements_heuristic(body)]
        skill_requirements = expand_skill_requirements(
            requirements,
            lambda value: [
                match.canonical_name
                for match in taxonomy.find_in_text(value)
                if match.canonical_name
            ],
        )
        coverage = skill_coverage(candidate.skills, skill_requirements)

        if requirements and bullet_vectors:
            requirement_vectors = dict(
                zip(
                    [requirement.text for requirement in requirements],
                    embedder.encode([requirement.text for requirement in requirements]),
                    strict=True,
                )
            )

            def similarity(requirement_text: str, bullet: str) -> float:
                left = requirement_vectors.get(requirement_text)
                right = bullet_vectors.get(bullet)
                return max(0.0, cosine(left, right)) if left and right else 0.0

            alignment = requirement_alignment(requirements, bullet_texts, similarity)
        else:
            alignment = requirement_alignment(requirements, bullet_texts, lambda a, b: 0.0)

        fit, _ = seniority_fit(candidate.seniority_level, posting.seniority_level)
        # Document-level similarity stands in for the cross-encoder here: the
        # control asks about the score, not about one stage's model.
        posting_vector = embedder.encode([f"{posting.title}\n{body[:4000]}"])[0]
        candidate_vector = embedder.encode(
            [" ".join(bullet_texts)[:4000] or " ".join(candidate.skills)]
        )[0]
        semantic = max(0.0, cosine(posting_vector, candidate_vector))

        unavailable = [] if skill_requirements else ["skill_coverage"]
        if not requirements:
            unavailable.append("requirement_alignment")

        return aggregate(
            SubScores(
                skill_coverage=coverage.score,
                requirement_alignment=alignment.score,
                seniority_fit=fit,
                semantic_similarity=semantic,
                freshness=freshness(posting.posted_at, now=now),
            ),
            GateResult(passed=True, failures=()),
            weights,
            unavailable=set(unavailable),
        ).total

    return score


def _true_pairs(
    candidates: Sequence[CandidateSnapshot],
    in_domain: Sequence[PostingSnapshot],
    out_of_domain: Sequence[PostingSnapshot],
) -> list[tuple[CandidateSnapshot, PostingSnapshot]]:
    """Pairs that genuinely belong together: engineers with engineering roles,
    the synthetic nurse and lawyer with out-of-domain roles."""
    pairs: list[tuple[CandidateSnapshot, PostingSnapshot]] = []
    real = [c for c in candidates if not c.profile_id.startswith("synthetic:")]
    fake = [c for c in candidates if c.profile_id.startswith("synthetic:")]

    for index, posting in enumerate(in_domain[:8]):
        if real:
            pairs.append((real[index % len(real)], posting))
    for index, posting in enumerate(out_of_domain[:8]):
        if fake:
            pairs.append((fake[index % len(fake)], posting))
    return pairs


def _postings_matching(
    session: Session, keywords: Sequence[str], *, limit: int
) -> list[PostingSnapshot]:
    rows = session.execute(
        text(
            """
            SELECT id, title, seniority_level, remote_type, locations, posted_at,
                   url_status, last_verified_at
              FROM job_postings
             WHERE status = 'open'
               AND lower(title) ~ :pattern
             ORDER BY posted_at DESC NULLS LAST
             LIMIT :limit
            """
        ),
        {"pattern": "|".join(keywords), "limit": limit},
    ).all()
    return [
        PostingSnapshot(
            posting_id=str(row.id),
            title=row.title,
            seniority_level=Seniority(row.seniority_level) if row.seniority_level else None,
            posted_at=row.posted_at,
        )
        for row in rows
    ]


SYNTHETIC_CANDIDATES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "synthetic:nurse",
        ("Communication", "Teamwork"),
        (
            "Delivered patient care on a 28-bed paediatric ward across day and night shifts",
            "Administered medication and maintained clinical records to trust standards",
            "Supported families through treatment plans and discharge planning",
        ),
    ),
    (
        "synthetic:lawyer",
        ("Communication", "Project Management"),
        (
            "Drafted and negotiated commercial contracts for corporate clients",
            "Managed disclosure across three multi-party litigation matters",
            "Advised on regulatory compliance and data protection obligations",
        ),
    ),
)


def _synthetic_candidates() -> list[CandidateSnapshot]:
    """Candidates from other disciplines, used only by the controls.

    Clearly marked synthetic in `profile_id`, never written to the database, and
    never scored for a real user.
    """
    return [
        CandidateSnapshot(
            profile_id=identifier,
            years_experience=6,
            seniority_level=Seniority.MID,
            skills=frozenset(skills),
            bullets=bullets,
        )
        for identifier, skills, bullets in SYNTHETIC_CANDIDATES
    ]


def _synthetic_out_of_domain() -> list[PostingSnapshot]:
    """Used when the corpus holds no out-of-domain roles to compare against."""
    return [
        PostingSnapshot(posting_id=f"synthetic-{index}", title=title)
        for index, title in enumerate(
            (
                "Registered Nurse, Paediatric Ward",
                "Paralegal, Corporate Litigation",
                "Head Chef, Hotel Restaurant",
                "Warehouse Operative, Night Shift",
            )
        )
    ]


def _seniority_ladder(template: PostingSnapshot | None) -> list[PostingSnapshot]:
    """One posting repeated at every seniority level (§9.4)."""
    base = template or PostingSnapshot(posting_id="synthetic-ladder", title="Data Engineer")
    return [replace_seniority(base, level) for level in SENIORITY_ORDER]


def replace_seniority(posting: PostingSnapshot, level: Seniority) -> PostingSnapshot:
    return posting.model_copy(update={"seniority_level": level})


def _load_candidate(session: Session, profile_id: uuid.UUID) -> CandidateSnapshot:
    row = session.execute(
        text("SELECT years_experience, seniority_level FROM candidate_profiles WHERE id = :id"),
        {"id": profile_id},
    ).one()
    skills = frozenset(
        session.execute(
            text(
                "SELECT s.canonical_name FROM profile_skills ps "
                "JOIN skills s ON s.id = ps.skill_id WHERE ps.profile_id = :id"
            ),
            {"id": profile_id},
        )
        .scalars()
        .all()
    )
    return CandidateSnapshot(
        profile_id=str(profile_id),
        years_experience=float(row.years_experience) if row.years_experience else None,
        seniority_level=Seniority(row.seniority_level) if row.seniority_level else None,
        skills=skills,
    )


def _load_bullets(session: Session, profile_id: uuid.UUID) -> list[tuple[str, list[float] | None]]:
    rows = session.execute(
        text("SELECT text, embedding FROM profile_bullets WHERE profile_id = :id ORDER BY ordinal"),
        {"id": profile_id},
    ).all()
    return [(row.text, parse_vector(row.embedding)) for row in rows]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
