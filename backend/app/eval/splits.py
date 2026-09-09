"""Cross-lingual evaluation splits (§18, Phase 5).

Phase 5's exit criterion is NDCG@10 on the Arabic split within 5 points of the
English split. That sentence hides a choice: what makes a *pair* Arabic — the
CV, the posting, or both?

All three, separately. The product's fifth promise (§1, point 5) is that Arabic
and English are handled "in a shared semantic space", and the cell that actually
tests that claim is the cross-lingual one: an Arabic CV against an English
posting, or the reverse. A system could score well on ar→ar by being a good
Arabic keyword matcher and still fail entirely at the thing being claimed. So
the report carries all four cells, and the headline comparison is the one the
plan names.

**A split with no labels produces no number.** Not zero, not the pooled figure —
`None`, and a report that says so. The golden set is human work (§9.1) and the
Arabic half of it does not exist yet; a metric invented to fill that gap would
be exactly the kind of number this codebase refuses to print elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.text.arabic import detect_language
from app.eval.metrics import RankingMetrics, evaluate_rankings

log = structlog.get_logger(__name__)

EXIT_CRITERION_POINTS = 5.0
"""§18, Phase 5: the Arabic split must be within this many NDCG points of the
English split. Points of NDCG, so 0.05 of the 0–1 metric."""

MIN_PAIRS_FOR_A_NUMBER = 20
"""Below this a split's NDCG is noise dressed as a measurement. Reported as
`insufficient`, which is a different thing from a bad score and must not be
allowed to read like one."""


@dataclass(slots=True)
class SplitResult:
    """One cell of the language matrix."""

    name: str
    cv_language: str | None
    posting_language: str | None
    profiles: int = 0
    pairs: int = 0
    metrics: RankingMetrics | None = None

    @property
    def is_measurable(self) -> bool:
        return self.metrics is not None and self.pairs >= MIN_PAIRS_FOR_A_NUMBER

    @property
    def ndcg(self) -> float | None:
        return self.metrics.ndcg_at_10 if self.is_measurable and self.metrics else None

    def as_row(self) -> dict[str, Any]:
        return {
            "split": self.name,
            "cv_language": self.cv_language,
            "posting_language": self.posting_language,
            "profiles": self.profiles,
            "pairs": self.pairs,
            "measurable": self.is_measurable,
            **(self.metrics.as_dict() if self.metrics else {}),
        }


@dataclass(slots=True)
class CrossLingualReport:
    splits: list[SplitResult] = field(default_factory=list)

    def by_name(self, name: str) -> SplitResult | None:
        return next((split for split in self.splits if split.name == name), None)

    @property
    def english(self) -> SplitResult | None:
        return self.by_name("en→en")

    @property
    def arabic(self) -> SplitResult | None:
        return self.by_name("ar→ar")

    @property
    def cross_lingual(self) -> list[SplitResult]:
        """The cells where the CV and the posting are in different scripts."""
        return [
            split
            for split in self.splits
            if split.cv_language
            and split.posting_language
            and split.cv_language != split.posting_language
        ]

    @property
    def gap_points(self) -> float | None:
        """English NDCG minus Arabic NDCG, in points. None if either is unmeasurable.

        Signed on purpose: an Arabic split that scores *higher* is not a pass to
        be celebrated, it is a result to explain — usually a split so small or
        so easy that the number means something other than what it says.
        """
        english, arabic = self.english, self.arabic
        if english is None or arabic is None:
            return None
        if english.ndcg is None or arabic.ndcg is None:
            return None
        return round((english.ndcg - arabic.ndcg) * 100, 2)

    @property
    def meets_exit_criterion(self) -> bool | None:
        """§18 Phase 5. None means not yet measurable — never False by default."""
        gap = self.gap_points
        if gap is None:
            return None
        return abs(gap) <= EXIT_CRITERION_POINTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "splits": [split.as_row() for split in self.splits],
            "gap_points": self.gap_points,
            "exit_criterion_points": EXIT_CRITERION_POINTS,
            "meets_exit_criterion": self.meets_exit_criterion,
        }

    def markdown_table(self) -> str:
        lines = [
            "| Split | Profiles | Pairs | NDCG@10 | MRR | P@5 |",
            "|---|---|---|---|---|---|",
        ]
        for split in self.splits:
            if split.is_measurable and split.metrics:
                metrics = split.metrics
                lines.append(
                    f"| {split.name} | {split.profiles} | {split.pairs} | "
                    f"{metrics.ndcg_at_10:.3f} | {metrics.mrr:.3f} | "
                    f"{metrics.precision_at_5:.3f} |"
                )
            else:
                reason = "no labels" if split.pairs == 0 else f"only {split.pairs} pairs"
                lines.append(
                    f"| {split.name} | {split.profiles} | {split.pairs} | — ({reason}) | — | — |"
                )

        gap = self.gap_points
        if gap is None:
            lines.append("")
            lines.append(
                "The Arabic split is not yet measurable, so the Phase 5 exit criterion "
                "is neither met nor missed. Labelling it is human work (§9.1)."
            )
        else:
            verdict = "within" if self.meets_exit_criterion else "outside"
            lines.append("")
            lines.append(
                f"English − Arabic = {gap:+.2f} NDCG points, {verdict} the "
                f"{EXIT_CRITERION_POINTS:.0f}-point exit criterion."
            )
        return "\n".join(lines)


def evaluate_by_language(
    rankings: Mapping[str, Sequence[str]],
    labels: Mapping[str, Mapping[str, int]],
    *,
    posting_languages: Mapping[str, str],
    profile_languages: Mapping[str, str],
) -> CrossLingualReport:
    """Score each language cell over the same rankings.

    Each cell restricts both the ranking and the labels to postings in that
    cell, so NDCG is computed over the ranking the candidate would actually see
    if only those postings existed. Filtering the labels but not the ranking
    would leave unlabelled postings occupying the top positions and quietly
    depress every split.
    """
    report = CrossLingualReport()

    for cv_language in ("en", "ar"):
        for posting_language in ("en", "ar"):
            split = SplitResult(
                name=f"{cv_language}→{posting_language}",
                cv_language=cv_language,
                posting_language=posting_language,
            )
            cell_labels: dict[str, dict[str, int]] = {}
            cell_rankings: dict[str, list[str]] = {}

            for profile_id, pairs in labels.items():
                if profile_languages.get(profile_id, "en") != cv_language:
                    continue
                kept = {
                    posting_id: grade
                    for posting_id, grade in pairs.items()
                    if posting_languages.get(posting_id, "en") == posting_language
                }
                if not kept:
                    continue
                cell_labels[profile_id] = kept
                cell_rankings[profile_id] = [
                    posting_id
                    for posting_id in rankings.get(profile_id, [])
                    if posting_languages.get(posting_id, "en") == posting_language
                ]

            split.profiles = len(cell_labels)
            split.pairs = sum(len(pairs) for pairs in cell_labels.values())
            if cell_labels:
                split.metrics = evaluate_rankings(cell_rankings, cell_labels)
            report.splits.append(split)

    return report


def posting_languages(session: Session) -> dict[str, str]:
    """Language per posting, from the stored column with a text fallback.

    `job_postings.language` is set at ingestion, but the column is nullable and
    older rows predate it. Falling back to detection means an evaluation is not
    silently English-only because a backfill has not run.
    """
    rows = session.execute(
        text("SELECT id, language, title, description_text FROM job_postings")
    ).all()
    return {
        str(row.id): row.language
        or detect_language(f"{row.title}\n{(row.description_text or '')[:2000]}")
        for row in rows
    }


def profile_languages(session: Session) -> dict[str, str]:
    """Language per candidate profile, detected from the CV text it was built from."""
    rows = session.execute(
        text(
            """
            SELECT p.id, v.raw_text
              FROM candidate_profiles p
              JOIN cv_versions v ON v.id = p.cv_version_id
            """
        )
    ).all()
    return {str(row.id): detect_language(row.raw_text or "") for row in rows}
