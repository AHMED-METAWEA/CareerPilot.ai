"""The labelled benchmark (§9.1).

The golden set is human work: 5 real CVs, 60 postings each, graded 0–3 against
a written rubric by three annotators. Nothing here generates labels — a
benchmark a machine wrote to grade itself measures nothing.

What this module provides is the shape: a stratified sampler that picks which
pairs to label (sampling across score deciles, because labelling only obvious
matches measures nothing either), storage in `eval_labels`, and the agreement
check that gates the whole thing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.eval.metrics import cohens_kappa, fleiss_kappa

log = structlog.get_logger(__name__)

KAPPA_GATE = 0.60
"""§9.1: below this the rubric is defective and no metric is trusted."""

RUBRIC = """\
Grade each candidate–posting pair 0–3. Grade the *fit*, not the candidate.

3 — Strong fit. The candidate meets the must-have requirements and the
    seniority is right. You would tell them to apply today.
2 — Worth applying to. Most must-haves are met; the gaps are learnable or the
    posting is flexible. A reasonable use of two hours.
1 — Related but not worth applying. Same field, wrong level, wrong
    specialisation, or a must-have they clearly lack.
0 — Not a fit. Different discipline, or a hard disqualifier such as work
    authorisation or an on-site requirement they cannot meet.

Rules:
- Judge from the posting text and the CV only. Do not research the company.
- A posting you cannot assess is a 1, not a 0.
- Seniority mismatch alone caps a pair at 1.
- Ignore salary, brand and how much you like the company.
"""


@dataclass(slots=True)
class LabelStats:
    pairs: int = 0
    labellers: int = 0
    by_grade: dict[int, int] = field(default_factory=dict)
    cohens_kappa: float | None = None
    fleiss_kappa: float | None = None

    @property
    def passes_gate(self) -> bool:
        agreement = self.fleiss_kappa if self.fleiss_kappa is not None else self.cohens_kappa
        return agreement is not None and agreement >= KAPPA_GATE


def sample_for_labelling(
    session: Session, profile_id: uuid.UUID, *, per_decile: int = 6
) -> list[dict[str, object]]:
    """Stratified sample across score deciles (§9.1).

    Sampling the top of the ranking would measure how good the system is at the
    cases it already thinks are good. The deciles are what put genuinely
    borderline pairs — the ones that decide a metric — in front of an annotator.
    """
    rows = session.execute(
        text(
            """
            WITH scored AS (
                SELECT m.posting_id, m.total_score,
                       ntile(10) OVER (ORDER BY m.total_score DESC) AS decile
                  FROM matches m
                 WHERE m.profile_id = :profile_id AND m.gate_passed
            ),
            sampled AS (
                SELECT posting_id, total_score, decile,
                       row_number() OVER (PARTITION BY decile ORDER BY random()) AS pick
                  FROM scored
            )
            SELECT s.posting_id, s.total_score, s.decile, p.title,
                   c.canonical_name AS company, p.apply_url
              FROM sampled s
              JOIN job_postings p ON p.id = s.posting_id
              LEFT JOIN companies c ON c.id = p.company_id
             WHERE s.pick <= :per_decile
             ORDER BY s.decile, s.total_score DESC
            """
        ),
        {"profile_id": profile_id, "per_decile": per_decile},
    ).all()
    return [dict(row._mapping) for row in rows]


def record_label(
    session: Session,
    *,
    profile_id: uuid.UUID,
    posting_id: uuid.UUID,
    grade: int,
    labeller: str,
) -> None:
    if grade not in (0, 1, 2, 3):
        raise ValueError(f"grade must be 0-3, got {grade}")
    session.execute(
        text(
            """
            INSERT INTO eval_labels (profile_id, posting_id, grade, labeler)
            VALUES (:profile_id, :posting_id, :grade, :labeller)
            ON CONFLICT (profile_id, posting_id, labeler)
            DO UPDATE SET grade = EXCLUDED.grade, labeled_at = now()
            """
        ),
        {
            "profile_id": profile_id,
            "posting_id": posting_id,
            "grade": grade,
            "labeller": labeller,
        },
    )


def load_labels(session: Session) -> dict[str, dict[str, int]]:
    """Consensus labels per profile: the median grade across annotators.

    Median rather than mean: grades are ordinal, and a single dissenting
    annotator should not move a pair by half a grade.
    """
    rows = session.execute(
        text(
            """
            SELECT profile_id, posting_id,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY grade) AS grade
              FROM eval_labels
             GROUP BY profile_id, posting_id
            """
        )
    ).all()
    labels: dict[str, dict[str, int]] = {}
    for row in rows:
        labels.setdefault(str(row.profile_id), {})[str(row.posting_id)] = int(row.grade)
    return labels


def agreement(session: Session) -> LabelStats:
    """Inter-annotator agreement, and the §9.1 gate that depends on it."""
    rows = session.execute(
        text("SELECT profile_id, posting_id, labeler, grade FROM eval_labels")
    ).all()
    stats = LabelStats()
    if not rows:
        return stats

    by_pair: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        by_pair.setdefault((str(row.profile_id), str(row.posting_id)), {})[row.labeler] = row.grade

    labellers = sorted({row.labeler for row in rows})
    stats.pairs = len(by_pair)
    stats.labellers = len(labellers)
    for row in rows:
        stats.by_grade[row.grade] = stats.by_grade.get(row.grade, 0) + 1

    if len(labellers) == 2:
        first, second = labellers
        shared = [
            (grades[first], grades[second])
            for grades in by_pair.values()
            if first in grades and second in grades
        ]
        if shared:
            stats.cohens_kappa = cohens_kappa(
                [pair[0] for pair in shared], [pair[1] for pair in shared]
            )
    elif len(labellers) > 2:
        complete = [
            [grades[labeller] for labeller in labellers]
            for grades in by_pair.values()
            if all(labeller in grades for labeller in labellers)
        ]
        if complete:
            stats.fleiss_kappa = fleiss_kappa(complete)

    return stats
