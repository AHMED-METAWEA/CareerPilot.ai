"""The evaluation harness (§9).

Runs the real pipeline over the labelled benchmark, computes the §9.2 metrics,
and produces the §9.3 ablation table. It also enforces the regression gate: a
drop of more than two points in NDCG@10 fails the build (§9.6).

Two rules keep the numbers honest:

* **The ablation runs this pipeline, not a copy of it.** Each configuration is
  `PipelineSettings` with stages switched off, so the table describes the system
  that ships.
* **Every result is written to `model_runs`.** A metric that exists only in a
  terminal cannot be compared with last week's.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.embeddings import build_embedder
from app.adapters.embeddings.base import EmbeddingBackend
from app.config import AppConfig
from app.domain.matching.rerank import Reranker
from app.eval.golden import load_labels
from app.eval.metrics import RankingMetrics, evaluate_rankings
from app.services.matching import MatchingService, PipelineSettings

log = structlog.get_logger(__name__)

REGRESSION_TOLERANCE = 2.0
"""Points of NDCG@10. §9.6: a larger drop fails the build."""

ABLATION_CONFIGURATIONS: tuple[PipelineSettings, ...] = (
    PipelineSettings(
        use_lexical=False, use_rerank=False, use_gates=False, use_decomposed_scoring=False
    ),
    PipelineSettings(use_rerank=False, use_gates=False, use_decomposed_scoring=False),
    PipelineSettings(use_gates=False, use_decomposed_scoring=False),
    PipelineSettings(),
)


@dataclass(slots=True)
class ConfigurationResult:
    label: str
    metrics: RankingMetrics
    p95_latency_ms: float
    settings: dict[str, bool]

    def as_row(self) -> dict[str, Any]:
        return {
            "configuration": self.label,
            **self.metrics.as_dict(),
            "p95_latency_ms": self.p95_latency_ms,
        }


@dataclass(slots=True)
class HarnessReport:
    results: list[ConfigurationResult] = field(default_factory=list)
    labelled_profiles: int = 0
    labelled_pairs: int = 0

    @property
    def headline(self) -> ConfigurationResult | None:
        """The full pipeline — the configuration that actually ships."""
        return self.results[-1] if self.results else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "labelled_profiles": self.labelled_profiles,
            "labelled_pairs": self.labelled_pairs,
            "ablation": [result.as_row() for result in self.results],
        }

    def markdown_table(self) -> str:
        """The §9.3 artefact, in the form the plan publishes it."""
        lines = [
            "| Configuration | NDCG@10 | MRR | P@5 | Recall(3)@10 | p95 latency |",
            "|---|---|---|---|---|---|",
        ]
        for result in self.results:
            metrics = result.metrics
            lines.append(
                f"| {result.label} | {metrics.ndcg_at_10:.3f} | {metrics.mrr:.3f} | "
                f"{metrics.precision_at_5:.3f} | {metrics.recall_grade3_at_10:.3f} | "
                f"{result.p95_latency_ms:.0f} ms |"
            )
        return "\n".join(lines)


class EvaluationHarness:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        embedder: EmbeddingBackend | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.embedder = embedder or build_embedder(config.models.embedding)
        self.reranker = reranker

    def run(
        self, configurations: Sequence[PipelineSettings] = ABLATION_CONFIGURATIONS
    ) -> HarnessReport:
        labels = load_labels(self.session)
        report = HarnessReport(
            labelled_profiles=len(labels),
            labelled_pairs=sum(len(pairs) for pairs in labels.values()),
        )
        if not labels:
            log.warning(
                "eval.no_labels",
                detail="the golden set is human work; run `careerpilot eval sample` first",
            )
            return report

        for settings in configurations:
            rankings: dict[str, list[str]] = {}
            latencies: list[float] = []

            for profile_id in labels:
                started = time.monotonic()
                rankings[profile_id] = self._rank(uuid.UUID(profile_id), settings)
                latencies.append((time.monotonic() - started) * 1000)

            report.results.append(
                ConfigurationResult(
                    label=settings.label,
                    metrics=evaluate_rankings(rankings, labels),
                    p95_latency_ms=round(_percentile(latencies, 95), 1),
                    settings={
                        "lexical": settings.use_lexical,
                        "vector": settings.use_vector,
                        "rerank": settings.use_rerank,
                        "gates": settings.use_gates,
                        "decomposed": settings.use_decomposed_scoring,
                    },
                )
            )

        self._record(report)
        return report

    def _rank(self, profile_id: uuid.UUID, settings: PipelineSettings) -> list[str]:
        """Run the pipeline for one profile and return its ranking.

        The run is rolled back: evaluating must not leave matches behind that
        the product would then serve.
        """
        savepoint = self.session.begin_nested()
        try:
            service = MatchingService(
                self.session,
                self.config,
                embedder=self.embedder,
                reranker=self.reranker,
                llm=None,  # the harness measures the deterministic path
                pipeline=settings,
            )
            service.run_for_profile(profile_id)
            rows = (
                self.session.execute(
                    text(
                        """
                    SELECT posting_id FROM matches
                     WHERE profile_id = :profile_id AND gate_passed
                     ORDER BY total_score DESC
                    """
                    ),
                    {"profile_id": profile_id},
                )
                .scalars()
                .all()
            )
            return [str(posting_id) for posting_id in rows]
        finally:
            savepoint.rollback()

    def _record(self, report: HarnessReport) -> None:
        """Every run lands in `model_runs`, so weeks can be compared (§9.6)."""
        headline = report.headline
        if headline is None:
            return
        self.session.execute(
            text(
                """
                INSERT INTO model_runs (component, model, version, params, metrics)
                VALUES ('matching', :model, :version, CAST(:params AS jsonb),
                        CAST(:metrics AS jsonb))
                """
            ),
            {
                "model": self.embedder.model_id,
                "version": "eval-1",
                "params": json.dumps(
                    {
                        "reranker": getattr(self.reranker, "model_id", None),
                        "configurations": [result.label for result in report.results],
                    }
                ),
                "metrics": json.dumps(report.as_dict()),
            },
        )

    # ── regression gate (§9.6) ───────────────────────────────────────

    def previous_ndcg(self) -> float | None:
        """NDCG@10 from the last recorded evaluation, if there is one."""
        row = self.session.execute(
            text(
                """
                SELECT metrics FROM model_runs
                 WHERE component = 'matching' AND version = 'eval-1'
                 ORDER BY run_at DESC OFFSET 1 LIMIT 1
                """
            )
        ).scalar_one_or_none()
        if not row:
            return None
        ablation = row.get("ablation") or []
        return float(ablation[-1]["ndcg@10"]) if ablation else None


def check_regression(
    current: float, previous: float | None, *, tolerance: float = REGRESSION_TOLERANCE
) -> tuple[bool, str]:
    """§9.6: a drop of more than `tolerance` points fails the build.

    Points of NDCG, not percent: a fall from 0.75 to 0.73 is two points and is
    tolerated; 0.75 to 0.70 is five and is not.
    """
    if previous is None:
        return True, "no previous evaluation to compare against"
    drop = (previous - current) * 100
    # Epsilon because §9.6 says *more than* two points: 0.75 → 0.73 is exactly
    # two and must pass, but in binary it comes out as 2.0000000000000018.
    if drop > tolerance + 1e-9:
        return False, (
            f"NDCG@10 fell {drop:.1f} points ({previous:.3f} → {current:.3f}), "
            f"tolerance is {tolerance:.1f}"
        )
    direction = "rose" if drop < 0 else "fell"
    return True, f"NDCG@10 {direction} {abs(drop):.1f} points ({previous:.3f} → {current:.3f})"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * percentile / 100), len(ordered) - 1)
    return ordered[index]
