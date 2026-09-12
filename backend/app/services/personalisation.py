"""Per-user preference learning (§11.8, §18 Phase 6).

The domain module fits the model; this one decides whether the fit is allowed to
touch anybody's ranking. That decision has three gates, and a fit that passes
two of them still changes nothing:

1. **200+ engagement events** (§11.8). Deliberate acts only — saves, dismissals,
   applications. Views are exposure, not preference.
2. **A fit that converges on both classes.** Enforced in the domain.
3. **Measured improvement on that user's own labelled subset.** Not on the
   global golden set: the claim being tested is "this helps *this* person", and
   a global metric cannot answer it.

Gate 3 is the one that does the real work, and it is also the one most likely to
fail. That is the intended outcome, not a defect: most users' engagement will
not carry enough signal to beat a default weighting that was itself tuned
against labelled data. A personalisation system that activates for everybody is
a personalisation system that is not checking anything.

Every rejected fit is stored with its reason, so the next run can say "tried,
did not help" instead of quietly re-deriving it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.scoring.aggregate import ScoreWeights
from app.domain.scoring.preferences import (
    FEATURES,
    MIN_EVENTS,
    Coefficients,
    FitReport,
    NotEnoughSignal,
    build_examples,
    fit_logistic,
    personalised_weights,
)
from app.eval.metrics import ndcg_at_k

log = structlog.get_logger(__name__)

MIN_IMPROVEMENT = 0.01
"""NDCG@10 gain required before personalised weights are applied.

A gain smaller than this is inside the noise of a few hundred labels. Shipping
it would mean telling a candidate their list is personalised on the strength of
a difference nobody could reproduce."""

MIN_LABELLED_PAIRS = 20
"""§11.8 validates against the user's *own* labelled subset. Below this there is
no subset to validate against, and the fit is held rather than guessed at."""


class PersonalisationError(Exception):
    pass


@dataclass(slots=True)
class PersonalisationOutcome:
    """What happened, in terms that can be shown to the user it describes."""

    user_id: uuid.UUID
    events: int
    applied: bool
    reason: str
    report: FitReport | None = None
    ndcg_default: float | None = None
    ndcg_personalised: float | None = None

    @property
    def improvement(self) -> float | None:
        if self.ndcg_default is None or self.ndcg_personalised is None:
            return None
        return round(self.ndcg_personalised - self.ndcg_default, 4)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "user_id": str(self.user_id),
            "events": self.events,
            "applied": self.applied,
            "reason": self.reason,
            "ndcg_default": self.ndcg_default,
            "ndcg_personalised": self.ndcg_personalised,
            "improvement": self.improvement,
        }
        if self.report is not None:
            payload["fit"] = self.report.as_dict()
        return payload


class PersonalisationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── reading ──────────────────────────────────────────────────────

    def weights_for(self, user_id: uuid.UUID) -> ScoreWeights | None:
        """The user's active personalised weights, or None for the defaults.

        Returns None rather than the defaults so the caller can tell the
        difference between "this user is personalised, and their weights happen
        to resemble the defaults" and "this user is not personalised".
        """
        row = self.session.execute(
            text("SELECT weights FROM user_score_weights WHERE user_id = :user_id AND is_active"),
            {"user_id": user_id},
        ).first()
        if row is None:
            return None

        stored = row.weights or {}
        missing = [name for name in FEATURES if name not in stored]
        if missing:
            # A row written by an older shape of the model. Falling back to the
            # defaults is right: a partial weighting is not a weighting.
            log.warning("personalisation.incomplete_weights", user_id=str(user_id), missing=missing)
            return None
        return ScoreWeights(**{name: float(stored[name]) for name in FEATURES})

    def explain(self, user_id: uuid.UUID) -> dict[str, object] | None:
        """Everything held about this user's personalisation, for §11.8's first
        constraint: a candidate can see why their list is weighted as it is."""
        row = self.session.execute(
            text(
                """
                SELECT weights, coefficients, adjustments, events_used, is_active,
                       ndcg_default, ndcg_personalised, rejected_reason, trained_at
                  FROM user_score_weights WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).first()
        if row is None:
            return None
        return {
            "active": row.is_active,
            "weights": row.weights,
            "coefficients": row.coefficients,
            "adjustments": row.adjustments,
            "events_used": row.events_used,
            "ndcg_default": float(row.ndcg_default) if row.ndcg_default is not None else None,
            "ndcg_personalised": (
                float(row.ndcg_personalised) if row.ndcg_personalised is not None else None
            ),
            "rejected_reason": row.rejected_reason,
            "trained_at": row.trained_at.isoformat() if row.trained_at else None,
        }

    # ── training ─────────────────────────────────────────────────────

    def train(self, user_id: uuid.UUID) -> PersonalisationOutcome:
        """Fit, validate, and persist — applying the weights only if they help."""
        rows = self._engagement_rows(user_id)
        examples = build_examples(rows)
        events = len(examples)

        if events < MIN_EVENTS:
            return PersonalisationOutcome(
                user_id=user_id,
                events=events,
                applied=False,
                reason=(
                    f"{events} deliberate engagement events; personalisation activates "
                    f"at {MIN_EVENTS} (§11.8)"
                ),
            )

        try:
            coefficients = fit_logistic(examples)
        except NotEnoughSignal as exc:
            return PersonalisationOutcome(
                user_id=user_id, events=events, applied=False, reason=str(exc)
            )

        weights, adjustments = personalised_weights(coefficients)
        positives = sum(1 for example in examples if example.label == 1)
        report = FitReport(
            coefficients=coefficients,
            examples=events,
            positives=positives,
            negatives=events - positives,
            weights=weights,
            adjustments=adjustments,
        )

        default_ndcg, personalised_ndcg, detail = self._validate(user_id, weights)
        if default_ndcg is None or personalised_ndcg is None:
            self._store(user_id, report, events, None, None, active=False, reason=detail)
            return PersonalisationOutcome(
                user_id=user_id, events=events, applied=False, reason=detail, report=report
            )

        improvement = personalised_ndcg - default_ndcg
        if improvement < MIN_IMPROVEMENT:
            reason = (
                f"personalised weights scored {personalised_ndcg:.4f} against "
                f"{default_ndcg:.4f} for the defaults — a gain of {improvement:+.4f}, "
                f"below the {MIN_IMPROVEMENT} required to apply them"
            )
            self._store(
                user_id,
                report,
                events,
                default_ndcg,
                personalised_ndcg,
                active=False,
                reason=reason,
            )
            log.info("personalisation.rejected", user_id=str(user_id), improvement=improvement)
            return PersonalisationOutcome(
                user_id=user_id,
                events=events,
                applied=False,
                reason=reason,
                report=report,
                ndcg_default=default_ndcg,
                ndcg_personalised=personalised_ndcg,
            )

        self._store(
            user_id, report, events, default_ndcg, personalised_ndcg, active=True, reason=None
        )
        log.info(
            "personalisation.applied",
            user_id=str(user_id),
            improvement=round(improvement, 4),
            strongest=coefficients.strongest(),
        )
        return PersonalisationOutcome(
            user_id=user_id,
            events=events,
            applied=True,
            reason=(
                f"applied: NDCG@10 on this user's own labels rose from "
                f"{default_ndcg:.4f} to {personalised_ndcg:.4f}"
            ),
            report=report,
            ndcg_default=default_ndcg,
            ndcg_personalised=personalised_ndcg,
        )

    # ── internals ────────────────────────────────────────────────────

    def _engagement_rows(
        self, user_id: uuid.UUID
    ) -> list[tuple[dict[str, float], str, int | None]]:
        """Every deliberate engagement joined to the score it was shown with.

        Joined to `matches` rather than to `job_postings`: the features are the
        sub-scores the user was actually presented with at the time, and a
        posting with no match row was never ranked for this user at all.
        """
        rows = self.session.execute(
            text(
                """
                SELECT e.event, m.subscores, m.rank
                  FROM user_job_events e
                  JOIN matches m
                    ON m.posting_id = e.posting_id AND m.user_id = e.user_id
                 WHERE e.user_id = :user_id
                 ORDER BY e.created_at
                """
            ),
            {"user_id": user_id},
        ).all()
        return [
            ({name: float(row.subscores.get(name, 0.0)) for name in FEATURES}, row.event, row.rank)
            for row in rows
            if row.subscores
        ]

    def _validate(
        self, user_id: uuid.UUID, weights: ScoreWeights
    ) -> tuple[float | None, float | None, str]:
        """Re-rank this user's labelled pairs under both weightings (§11.8).

        Rescoring the stored sub-scores rather than re-running the pipeline is
        deliberate and is what makes the comparison fair: retrieval, gates and
        reranking are held fixed, so the only thing that differs between the two
        numbers is the weighting under test.
        """
        rows = self.session.execute(
            text(
                """
                SELECT l.posting_id, l.grade, m.subscores, m.gate_passed
                  FROM eval_labels l
                  JOIN candidate_profiles p ON p.id = l.profile_id
                  JOIN matches m ON m.posting_id = l.posting_id AND m.profile_id = l.profile_id
                 WHERE p.user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).all()

        usable = [row for row in rows if row.subscores and row.gate_passed]
        if len(usable) < MIN_LABELLED_PAIRS:
            return (
                None,
                None,
                (
                    f"{len(usable)} labelled pairs for this user; §11.8 requires validation "
                    f"against their own labels and {MIN_LABELLED_PAIRS} is the floor"
                ),
            )

        labels = {str(row.posting_id): int(row.grade) for row in usable}

        def ranked(scoring: ScoreWeights) -> list[str]:
            scored = [
                (
                    str(row.posting_id),
                    sum(
                        getattr(scoring, name) * float(row.subscores.get(name, 0.0))
                        for name in FEATURES
                    ),
                )
                for row in usable
            ]
            # Tie-break on posting id so the comparison is not decided by row
            # order: two weightings that produce identical scores must produce
            # identical rankings, or the "improvement" is an artefact.
            scored.sort(key=lambda pair: (-pair[1], pair[0]))
            return [posting_id for posting_id, _ in scored]

        default_ndcg = ndcg_at_k(ranked(ScoreWeights()), labels, 10)
        personalised_ndcg = ndcg_at_k(ranked(weights), labels, 10)
        return default_ndcg, personalised_ndcg, "validated"

    def _store(
        self,
        user_id: uuid.UUID,
        report: FitReport,
        events: int,
        default_ndcg: float | None,
        personalised_ndcg: float | None,
        *,
        active: bool,
        reason: str | None,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO user_score_weights (
                    user_id, weights, coefficients, adjustments, events_used,
                    ndcg_default, ndcg_personalised, is_active, rejected_reason, trained_at
                ) VALUES (
                    :user_id, CAST(:weights AS jsonb), CAST(:coefficients AS jsonb),
                    CAST(:adjustments AS jsonb), :events, :ndcg_default, :ndcg_personalised,
                    :active, :reason, now()
                )
                ON CONFLICT (user_id) DO UPDATE SET
                    weights = EXCLUDED.weights,
                    coefficients = EXCLUDED.coefficients,
                    adjustments = EXCLUDED.adjustments,
                    events_used = EXCLUDED.events_used,
                    ndcg_default = EXCLUDED.ndcg_default,
                    ndcg_personalised = EXCLUDED.ndcg_personalised,
                    is_active = EXCLUDED.is_active,
                    rejected_reason = EXCLUDED.rejected_reason,
                    trained_at = EXCLUDED.trained_at
                """
            ),
            {
                "user_id": user_id,
                "weights": json.dumps({name: getattr(report.weights, name) for name in FEATURES}),
                "coefficients": json.dumps(
                    {
                        "values": report.coefficients.values,
                        "intercept": report.coefficients.intercept,
                        "rank_control": report.coefficients.rank_control,
                        "converged": report.coefficients.converged,
                        "log_loss": report.coefficients.log_loss,
                    }
                ),
                "adjustments": json.dumps(report.adjustments),
                "events": events,
                "ndcg_default": default_ndcg,
                "ndcg_personalised": personalised_ndcg,
                "active": active,
                "reason": reason,
            },
        )

    def deactivate(self, user_id: uuid.UUID) -> bool:
        """Return a user to the default weighting.

        §11.8 gives no mechanism for a candidate to refuse personalisation, but
        a ranking that changed for reasons the person did not choose needs an
        off switch, and the row is kept so the decision is visible afterwards.
        """
        updated = self.session.execute(
            text(
                "UPDATE user_score_weights SET is_active = false, "
                "rejected_reason = 'turned off by the candidate' "
                "WHERE user_id = :user_id AND is_active "
                "RETURNING user_id"
            ),
            {"user_id": user_id},
        ).first()
        return updated is not None


def coefficients_from_row(stored: Mapping[str, Any]) -> Coefficients:
    """Rebuild a `Coefficients` from what `_store` wrote."""
    values = stored.get("values") or {}
    return Coefficients(
        values={name: float(values.get(name, 0.0)) for name in FEATURES},
        intercept=float(stored.get("intercept", 0.0)),
        rank_control=float(stored.get("rank_control", 0.0)),
        converged=bool(stored.get("converged", False)),
        log_loss=float(stored.get("log_loss", 0.0)),
    )
