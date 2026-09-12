"""Matching — W4 (§11.4).

    STAGE 2  Gates          ~3,200 open  →  ~400 eligible
    STAGE 3  Retrieval      BM25 ∪ vector → RRF → ~120
    STAGE 4  Rerank         cross-encoder on 120 → 25
    STAGE 5  Analysis       requirements, alignment, deterministic scoring

The ordering is the whole design (§4.2): every stage that narrows the pool is
cheap and deterministic, and the only expensive stage runs on twenty-five
postings. That is what makes a nightly run fit inside a free tier — and it is
also why the funnel must never be reordered for convenience.

Nothing here lets a model produce a number that reaches a score. The model
extracts requirements and assesses them; the arithmetic is all in
`app.domain.scoring`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.embeddings.base import (
    EmbeddingBackend,
    cosine,
    parse_vector,
    to_pgvector,
)
from app.adapters.llm.base import ChatProvider, LLMError, complete_schema
from app.config import AppConfig
from app.domain.jobs.requirements import (
    REQUIREMENTS_SYSTEM_PROMPT,
    ExtractedRequirements,
    build_requirements_prompt,
    expand_skill_requirements,
    extract_requirements_heuristic,
    ground_requirements,
    sponsorship_stance,
    stated_min_years,
)
from app.domain.matching.gates import GateConfig, GateResult, evaluate_gates
from app.domain.matching.rerank import Reranker, rerank
from app.domain.matching.retrieval import RetrievalHit, reciprocal_rank_fusion
from app.domain.models import (
    CandidateSnapshot,
    LanguageAbility,
    PostingRequirement,
    PostingSnapshot,
    RemoteType,
    Seniority,
    UrlStatus,
    WorkAuthorization,
)
from app.domain.scoring.aggregate import ScoreWeights, SubScores, aggregate
from app.domain.scoring.percentile import percentile_of
from app.domain.scoring.subscores import (
    RequirementAlignment,
    freshness,
    normalize_semantic,
    requirement_alignment,
    seniority_fit,
    skill_coverage,
)
from app.services.taxonomy import load_taxonomy

log = structlog.get_logger(__name__)

MODEL_VERSION = "match-1"
GATE_SCAN_LIMIT = 5000
"""Postings pulled into stage 2 per run. The SQL pre-filter does the coarse work."""


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """Which stages of the funnel are active.

    Exists so the §9.3 ablation measures *this* pipeline rather than a
    reimplementation of it — the configurations in the published table are this
    same code with stages switched off.
    """

    use_lexical: bool = True
    """BM25 over tsvector — the half that catches exact tool names."""
    use_vector: bool = True
    """pgvector similarity — the half that catches paraphrase and other languages."""
    use_rerank: bool = True
    use_gates: bool = True
    use_decomposed_scoring: bool = True
    """When false, the score is the retrieval/rerank similarity alone — the
    'document cosine only' baseline the plan rejects (§8.1)."""
    use_personalisation: bool = True
    """Per-user weights (§11.8). Off in the ablation harness, which must measure
    the pipeline and not whichever users happen to have earned a fit."""

    @property
    def label(self) -> str:
        if not self.use_decomposed_scoring and not self.use_rerank and not self.use_lexical:
            return "document cosine only"
        if not self.use_decomposed_scoring and not self.use_rerank:
            return "+ BM25 fusion (RRF)"
        if not self.use_decomposed_scoring:
            return "+ cross-encoder rerank"
        return "+ decomposed scorer with gates"


@dataclass(slots=True)
class MatchRunReport:
    profile_id: uuid.UUID
    scanned: int = 0
    eligible: int = 0
    retrieved: int = 0
    finalists: int = 0
    persisted: int = 0
    withheld: int = 0
    gate_failures: dict[str, int] = field(default_factory=dict)
    used_llm: bool = False
    tokens_in: int = 0
    tokens_out: int = 0


class MatchingService:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        embedder: EmbeddingBackend,
        reranker: Reranker | None = None,
        llm: ChatProvider | None = None,
        pipeline: PipelineSettings | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.embedder = embedder
        self.reranker = reranker
        self.llm = llm
        self.pipeline = pipeline or PipelineSettings()
        # Per-run, per-instance: a class attribute here would leak one
        # candidate's rerank scores into another candidate's normalisation.
        self._rerank_scores: dict[str, float] = {}
        self._taxonomy = load_taxonomy(
            session, fuzzy_threshold=config.scoring.skill_fuzzy_threshold
        )

    # ── entry point ──────────────────────────────────────────────────

    def run_for_profile(self, profile_id: uuid.UUID) -> MatchRunReport:
        report = MatchRunReport(profile_id=profile_id)
        now = datetime.now(UTC)

        candidate, user_id, bullets = self._load_candidate(profile_id)

        eligible, withheld = self._stage2_gates(candidate, report, now)
        report.eligible = len(eligible)
        report.withheld = len(withheld)
        if not eligible:
            log.info("match.no_eligible_postings", profile_id=str(profile_id))
            self._persist_withheld(user_id, profile_id, withheld)
            return report

        fused = self._stage3_retrieval(candidate, list(eligible))
        report.retrieved = len(fused)

        finalists = self._stage4_rerank(candidate, fused)
        report.finalists = len(finalists)

        scored = self._stage5_analysis(
            candidate, finalists, eligible, bullets, now, report, user_id=user_id
        )
        report.persisted = self._persist(user_id, profile_id, scored)
        self._persist_withheld(user_id, profile_id, withheld)

        log.info(
            "match.completed",
            profile_id=str(profile_id),
            scanned=report.scanned,
            eligible=report.eligible,
            retrieved=report.retrieved,
            finalists=report.finalists,
            persisted=report.persisted,
            withheld=report.withheld,
            used_llm=report.used_llm,
        )
        return report

    # ── stage 2: gates ───────────────────────────────────────────────

    def _stage2_gates(
        self, candidate: CandidateSnapshot, report: MatchRunReport, now: datetime
    ) -> tuple[dict[str, PostingSnapshot], list[tuple[PostingSnapshot, GateResult]]]:
        """Evaluate every gate, keeping the reasons for what was withheld."""
        gate_config = GateConfig(
            max_years_shortfall=self.config.gates.max_years_shortfall,
            max_posting_age_days=self.config.gates.max_posting_age_days,
            require_verified_url=self.config.gates.require_verified_url,
            verification_window_hours=self.config.verification.suppress_after_hours,
        )

        rows = self.session.execute(
            text(
                """
                SELECT p.id, p.title, p.seniority_level, p.remote_type, p.locations,
                       p.posted_at, p.url_status, p.last_verified_at, p.description_text
                  FROM job_postings p
                 WHERE p.status = 'open'
                   AND (p.posted_at IS NULL
                        OR p.posted_at > now() - CAST(:max_age AS interval))
                 ORDER BY p.posted_at DESC NULLS LAST
                 LIMIT :limit
                """
            ),
            {
                "max_age": f"{self.config.gates.max_posting_age_days} days",
                "limit": GATE_SCAN_LIMIT,
            },
        ).all()
        report.scanned = len(rows)

        eligible: dict[str, PostingSnapshot] = {}
        withheld: list[tuple[PostingSnapshot, GateResult]] = []

        for row in rows:
            snapshot = _posting_snapshot(row)
            result = (
                evaluate_gates(candidate, snapshot, gate_config, now=now)
                if self.pipeline.use_gates
                else GateResult(passed=True, failures=())
            )
            if result.passed:
                eligible[snapshot.posting_id] = snapshot
            else:
                withheld.append((snapshot, result))
                for failure in result.failures:
                    report.gate_failures[failure.value] = (
                        report.gate_failures.get(failure.value, 0) + 1
                    )
        return eligible, withheld

    # ── stage 3: hybrid retrieval ────────────────────────────────────

    def _stage3_retrieval(self, candidate: CandidateSnapshot, eligible_ids: list[str]) -> list[str]:
        """BM25 and vector search over the eligible set, fused by RRF (§7.3)."""
        query = self._query_text(candidate)
        ids = [uuid.UUID(posting_id) for posting_id in eligible_ids]
        top_k = self.config.funnel.retrieval_top_k

        lexical = self.session.execute(
            text(
                """
                SELECT p.id,
                       ts_rank_cd(
                           to_tsvector('simple', p.title || ' ' || p.description_text),
                           plainto_tsquery('simple', :query)
                       ) AS rank
                  FROM job_postings p
                 WHERE p.id = ANY(:ids)
                   AND to_tsvector('simple', p.title || ' ' || p.description_text)
                       @@ plainto_tsquery('simple', :query)
                 ORDER BY rank DESC
                 LIMIT :limit
                """
            ),
            {"query": query, "ids": ids, "limit": top_k},
        ).all()

        profile_vector = self.embedder.encode([query])[0]
        vector = self.session.execute(
            text(
                """
                SELECT e.posting_id AS id,
                       1 - (e.embedding <=> CAST(:vector AS halfvec)) AS similarity
                  FROM job_embeddings e
                 WHERE e.posting_id = ANY(:ids) AND e.model = :model
                 ORDER BY e.embedding <=> CAST(:vector AS halfvec)
                 LIMIT :limit
                """
            ),
            {
                "vector": to_pgvector(profile_vector),
                "ids": ids,
                "model": self.embedder.model_id,
                "limit": top_k,
            },
        ).all()

        rankings: dict[str, list[RetrievalHit]] = {}
        if self.pipeline.use_lexical:
            rankings["bm25"] = [
                RetrievalHit(str(row.id), rank=index + 1, score=float(row.rank))
                for index, row in enumerate(lexical)
            ]
        if self.pipeline.use_vector:
            rankings["vector"] = [
                RetrievalHit(str(row.id), rank=index + 1, score=float(row.similarity))
                for index, row in enumerate(vector)
            ]
        fused = reciprocal_rank_fusion(
            rankings, k=self.config.funnel.rrf_k, limit=self.config.funnel.fusion_top_n
        )

        if not fused:
            # Neither retriever matched — a profile with no skills, or a corpus
            # not embedded yet. Fall back to freshness order rather than
            # returning nothing, and say so.
            log.warning("match.retrieval_empty", profile=candidate.profile_id)
            return eligible_ids[: self.config.funnel.fusion_top_n]
        return [hit.posting_id for hit in fused]

    # ── stage 4: rerank ──────────────────────────────────────────────

    def _stage4_rerank(self, candidate: CandidateSnapshot, posting_ids: list[str]) -> list[str]:
        if not posting_ids:
            return []
        rows = self.session.execute(
            text(
                "SELECT id, title, substring(description_text, 1, 2000) AS body "
                "FROM job_postings WHERE id = ANY(:ids)"
            ),
            {"ids": [uuid.UUID(posting_id) for posting_id in posting_ids]},
        ).all()
        # Preserve retrieval order: `rows` comes back in whatever order the
        # database chose, and an ablation with reranking off must measure
        # retrieval order rather than a shuffle.
        by_id = {str(row.id): f"{row.title}\n{row.body}" for row in rows}
        documents = [
            (posting_id, by_id[posting_id]) for posting_id in posting_ids if posting_id in by_id
        ]
        hits = rerank(
            self._query_text(candidate),
            documents,
            self.reranker if self.pipeline.use_rerank else None,
            top_n=self.config.funnel.rerank_top_n,
        )
        self._rerank_scores = {hit.posting_id: hit.score for hit in hits}
        return [hit.posting_id for hit in hits]

    def _weights_for(self, user_id: uuid.UUID | None) -> ScoreWeights:
        """The configured weights, or this user's personalised ones (§11.8).

        Personalisation is off unless a fit has been validated against that
        user's own labels, so the common path returns the configured defaults.
        The evaluation harness passes no user at all, which keeps an ablation
        measuring the pipeline rather than whoever happens to be personalised.
        """
        defaults = ScoreWeights(**self.config.scoring.weights.model_dump())
        if user_id is None or not self.pipeline.use_personalisation:
            return defaults

        from app.services.personalisation import PersonalisationService

        personalised = PersonalisationService(self.session).weights_for(user_id)
        if personalised is None:
            return defaults

        log.info("match.personalised_weights", user_id=str(user_id))
        return personalised

    # ── stage 5: analysis and scoring ────────────────────────────────

    def _stage5_analysis(
        self,
        candidate: CandidateSnapshot,
        finalists: list[str],
        eligible: Mapping[str, PostingSnapshot],
        bullets: list[tuple[str, list[float] | None]],
        now: datetime,
        report: MatchRunReport,
        *,
        user_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        weights = self._weights_for(user_id)
        rerank_pool = [self._rerank_scores.get(posting_id, 0.5) for posting_id in finalists]
        bullet_vectors = [vector for _, vector in bullets if vector is not None]
        bullet_texts = [text_value for text_value, vector in bullets if vector is not None]

        scored: list[dict[str, Any]] = []
        for posting_id in finalists:
            snapshot = eligible[posting_id]
            requirements = self._requirements_for(posting_id, report)
            # Coverage counts skills; evidence stays at sentence level.
            skill_requirements = expand_skill_requirements(
                requirements,
                lambda text_value: [
                    match.canonical_name
                    for match in self._taxonomy.find_in_text(text_value)
                    if match.canonical_name
                ],
            )

            coverage = skill_coverage(candidate.skills, skill_requirements)
            alignment = self._alignment(requirements, bullet_texts, bullet_vectors)
            fit, fit_reason = seniority_fit(candidate.seniority_level, snapshot.seniority_level)
            semantic = normalize_semantic(self._rerank_scores.get(posting_id, 0.5), rerank_pool)
            fresh = freshness(
                snapshot.posted_at,
                now=now,
                half_life_days=self.config.scoring.freshness_half_life_days,
            )

            subscores = SubScores(
                skill_coverage=coverage.score,
                requirement_alignment=alignment.score,
                seniority_fit=fit,
                semantic_similarity=semantic,
                freshness=fresh,
            )
            # A posting whose requirements could not be extracted tells us
            # nothing about coverage or alignment; those terms are dropped and
            # the rest renormalised rather than scored as a perfect match.
            unavailable: list[str] = []
            if not requirements:
                unavailable.extend(["skill_coverage", "requirement_alignment"])
            elif coverage.total_count == 0:
                # The posting names no skill this taxonomy knows. That is a gap
                # in the vocabulary, not evidence about the candidate.
                unavailable.append("skill_coverage")
            if not bullet_vectors:
                unavailable.append("requirement_alignment")

            if self.pipeline.use_decomposed_scoring:
                score = aggregate(
                    subscores,
                    GateResult(passed=True, failures=()),
                    weights,
                    unavailable=set(unavailable),
                )
            else:
                # The rejected baseline (§8.1): similarity alone, no gates, no
                # decomposition. Measured so the ablation can show what the rest
                # of the pipeline is worth.
                score = aggregate(
                    subscores,
                    GateResult(passed=True, failures=()),
                    ScoreWeights(0.0, 0.0, 0.0, 1.0, 0.0),
                )

            scored.append(
                {
                    "posting_id": posting_id,
                    "score": score,
                    "coverage": coverage,
                    "alignment": alignment,
                    "fit_reason": fit_reason,
                    "requirements": requirements,
                }
            )

        scored.sort(key=lambda item: -item["score"].total)
        return scored

    def _alignment(
        self,
        requirements: list[PostingRequirement],
        bullet_texts: list[str],
        bullet_vectors: list[list[float]],
    ) -> RequirementAlignment:
        """Requirement-level alignment over sentence vectors (§8.4)."""
        if not requirements or not bullet_vectors:
            return requirement_alignment(requirements, bullet_texts, lambda a, b: 0.0)

        requirement_vectors = self.embedder.encode([req.text for req in requirements])
        index = {
            req.text: vector for req, vector in zip(requirements, requirement_vectors, strict=True)
        }
        bullet_index = dict(zip(bullet_texts, bullet_vectors, strict=True))

        def similarity(requirement_text: str, bullet: str) -> float:
            left, right = index.get(requirement_text), bullet_index.get(bullet)
            if left is None or right is None:
                return 0.0
            # Cosine on unit vectors lands in [-1, 1]; negatives mean unrelated.
            return max(0.0, cosine(left, right))

        return requirement_alignment(requirements, bullet_texts, similarity)

    def _requirements_for(
        self, posting_id: str, report: MatchRunReport
    ) -> list[PostingRequirement]:
        """Extracted requirements for one posting, cached in `job_requirements`.

        The model path runs only for finalists, and only when a provider is
        configured; otherwise the deterministic path applies, which is also the
        ablation baseline.
        """
        stored = self.session.execute(
            text(
                "SELECT text, kind, is_must_have, skill_id FROM job_requirements "
                "WHERE posting_id = :id ORDER BY id"
            ),
            {"id": uuid.UUID(posting_id)},
        ).all()
        if stored:
            return [
                PostingRequirement(
                    text=row.text, kind=row.kind, is_must_have=row.is_must_have, skill=None
                )
                for row in stored
            ]

        row = self.session.execute(
            text("SELECT title, description_text FROM job_postings WHERE id = :id"),
            {"id": uuid.UUID(posting_id)},
        ).one()

        extracted: list[tuple[PostingRequirement, Any]] = []
        if self.llm is not None:
            try:
                result, usage = complete_schema(
                    self.llm,
                    system=REQUIREMENTS_SYSTEM_PROMPT,
                    user=build_requirements_prompt(row.title, row.description_text),
                    model=self.config.models.analysis.model,
                    schema=ExtractedRequirements,
                )
                extracted = ground_requirements(row.description_text, result)
                report.used_llm = True
                report.tokens_in += usage.tokens_in
                report.tokens_out += usage.tokens_out
            except LLMError as exc:
                log.warning("match.requirement_extraction_failed", error=str(exc))

        if not extracted:
            extracted = extract_requirements_heuristic(row.description_text)

        self._persist_requirements(uuid.UUID(posting_id), extracted)
        return [requirement for requirement, _ in extracted]

    def _persist_requirements(
        self, posting_id: uuid.UUID, extracted: list[tuple[PostingRequirement, Any]]
    ) -> None:
        for requirement, span in extracted:
            name = requirement.skill
            if not name:
                mentions = self._taxonomy.find_in_text(requirement.text, limit=1)
                name = mentions[0].canonical_name if mentions else None
            skill_id = None
            if name:
                skill_id = self.session.execute(
                    text("SELECT id FROM skills WHERE canonical_name = :name"),
                    {"name": name},
                ).scalar_one_or_none()
            self.session.execute(
                text(
                    """
                    INSERT INTO job_requirements (posting_id, text, kind, is_must_have,
                                                  skill_id, span)
                    VALUES (:posting_id, :text, :kind, :must, :skill_id,
                            CAST(:span AS int4range))
                    """
                ),
                {
                    "posting_id": posting_id,
                    "text": requirement.text,
                    "kind": requirement.kind,
                    "must": requirement.is_must_have,
                    "skill_id": skill_id,
                    "span": span.as_int4range() if span else "[0,0)",
                },
            )

    # ── persistence ──────────────────────────────────────────────────

    def _persist(
        self, user_id: uuid.UUID, profile_id: uuid.UUID, scored: list[dict[str, Any]]
    ) -> int:
        if not scored:
            return 0
        totals = [item["score"].total for item in scored]

        for rank, item in enumerate(scored, start=1):
            score = item["score"]
            coverage = item["coverage"]
            percentile = percentile_of(score.total, totals)
            match_id = self.session.execute(
                text(
                    """
                    INSERT INTO matches (user_id, profile_id, posting_id, total_score,
                                         percentile, gate_passed, gate_failures, subscores,
                                         rank, model_version, explanation, gaps)
                    VALUES (:user_id, :profile_id, :posting_id, :total, :percentile, true,
                            NULL, CAST(:subscores AS jsonb), :rank, :model_version,
                            :explanation, :gaps)
                    ON CONFLICT (profile_id, posting_id, model_version) DO UPDATE
                       SET total_score = EXCLUDED.total_score,
                           percentile = EXCLUDED.percentile,
                           subscores = EXCLUDED.subscores,
                           rank = EXCLUDED.rank,
                           explanation = EXCLUDED.explanation,
                           gaps = EXCLUDED.gaps,
                           computed_at = now()
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "posting_id": uuid.UUID(item["posting_id"]),
                    "total": score.total,
                    "percentile": percentile.value,
                    "subscores": json.dumps(
                        {**score.subscores.as_dict(), "contributions": score.contributions}
                    ),
                    "rank": rank,
                    "model_version": MODEL_VERSION,
                    "explanation": _explain(
                        coverage, item["fit_reason"], percentile, score.unavailable
                    ),
                    "gaps": list(coverage.missing_must_haves) or list(coverage.missing[:5]),
                },
            ).scalar_one()
            self._persist_evidence(match_id, uuid.UUID(item["posting_id"]), item["alignment"])
        return len(scored)

    def _persist_evidence(self, match_id: uuid.UUID, posting_id: uuid.UUID, alignment: Any) -> None:
        """One row per requirement, citing the bullet that justified the verdict."""
        requirement_ids = {
            row.text: row.id
            for row in self.session.execute(
                text("SELECT id, text FROM job_requirements WHERE posting_id = :id"),
                {"id": posting_id},
            ).all()
        }
        for evidence in alignment.evidence:
            requirement_id = requirement_ids.get(evidence.requirement)
            if requirement_id is None:
                continue
            self.session.execute(
                text(
                    """
                    INSERT INTO match_evidence (match_id, requirement_id, status, similarity, note)
                    VALUES (:match_id, :requirement_id, :status, :similarity, :note)
                    """
                ),
                {
                    "match_id": match_id,
                    "requirement_id": requirement_id,
                    "status": evidence.status,
                    "similarity": round(evidence.similarity, 3),
                    "note": evidence.best_bullet,
                },
            )

    def _persist_withheld(
        self,
        user_id: uuid.UUID,
        profile_id: uuid.UUID,
        withheld: list[tuple[PostingSnapshot, GateResult]],
    ) -> None:
        """Gated postings, with their reasons — this is what `/matches/withheld` reads.

        Capped: the point is to explain a pattern to the user, not to write a
        row for every posting on earth they cannot take.
        """
        for snapshot, result in withheld[:200]:
            self.session.execute(
                text(
                    """
                    INSERT INTO matches (user_id, profile_id, posting_id, total_score,
                                         gate_passed, gate_failures, subscores, model_version)
                    VALUES (:user_id, :profile_id, :posting_id, 0, false, :failures,
                            '{}'::jsonb, :model_version)
                    ON CONFLICT (profile_id, posting_id, model_version) DO UPDATE
                       SET gate_failures = EXCLUDED.gate_failures, computed_at = now()
                    """
                ),
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "posting_id": uuid.UUID(snapshot.posting_id),
                    "failures": [failure.value for failure in result.failures],
                    "model_version": MODEL_VERSION,
                },
            )

    # ── loading ──────────────────────────────────────────────────────

    def _load_candidate(
        self, profile_id: uuid.UUID
    ) -> tuple[CandidateSnapshot, uuid.UUID, list[tuple[str, list[float] | None]]]:
        row = self.session.execute(
            text(
                """
                SELECT user_id, years_experience, seniority_level, locations, work_auth,
                       languages, summary
                  FROM candidate_profiles WHERE id = :id
                """
            ),
            {"id": profile_id},
        ).one()

        skills = frozenset(
            self.session.execute(
                text(
                    """
                    SELECT s.canonical_name FROM profile_skills ps
                      JOIN skills s ON s.id = ps.skill_id
                     WHERE ps.profile_id = :id
                    """
                ),
                {"id": profile_id},
            )
            .scalars()
            .all()
        )

        bullet_rows = self.session.execute(
            text(
                "SELECT text, embedding FROM profile_bullets WHERE profile_id = :id "
                "ORDER BY ordinal"
            ),
            {"id": profile_id},
        ).all()
        bullets = [(row_.text, parse_vector(row_.embedding)) for row_ in bullet_rows]

        work_auth = tuple(
            WorkAuthorization(country=country, status=_auth_status(value))
            for country, value in (row.work_auth or {}).items()
        )
        languages = tuple(
            LanguageAbility(language=item.get("lang", ""), cefr=item.get("cefr"))
            for item in (row.languages or [])
            if item.get("lang")
        )
        locations = tuple(row.locations or ())

        candidate = CandidateSnapshot(
            profile_id=str(profile_id),
            years_experience=float(row.years_experience) if row.years_experience else None,
            seniority_level=Seniority(row.seniority_level) if row.seniority_level else None,
            locations=locations,
            countries=tuple(auth.country for auth in work_auth),
            work_authorization=work_auth,
            languages=languages,
            skills=skills,
            bullets=tuple(text_value for text_value, _ in bullets),
        )
        return candidate, uuid.UUID(str(row.user_id)), bullets

    def _query_text(self, candidate: CandidateSnapshot) -> str:
        """What the retrievers search with: skills first, then CV prose.

        Skills lead because the lexical retriever is the half that catches exact
        tool names, and that is precisely what it is there for (§7.3).
        """
        parts = [" ".join(sorted(candidate.skills))]
        parts.extend(candidate.bullets[:8])
        return " ".join(part for part in parts if part)[:4000]


def _posting_snapshot(row: Any) -> PostingSnapshot:
    locations = row.locations or []
    countries = tuple(location.get("country") for location in locations if location.get("country"))
    cities = tuple(location.get("city") for location in locations if location.get("city"))
    return PostingSnapshot(
        posting_id=str(row.id),
        title=row.title,
        seniority_level=Seniority(row.seniority_level) if row.seniority_level else None,
        remote_type=RemoteType(row.remote_type) if row.remote_type else None,
        countries=countries,
        cities=cities,
        posted_at=row.posted_at,
        url_status=UrlStatus(row.url_status) if row.url_status else UrlStatus.UNKNOWN,
        last_verified_at=row.last_verified_at,
        min_years=stated_min_years(row.description_text),
        offers_sponsorship=sponsorship_stance(row.description_text),
    )


def _auth_status(value: str) -> str:
    lowered = str(value).casefold()
    for status in ("citizen", "permanent_resident", "work_visa", "requires_sponsorship"):
        if status.replace("_", " ") in lowered or status in lowered:
            return status
    return "none"


def _explain(
    coverage: Any, fit_reason: str, percentile: Any, unavailable: tuple[str, ...] = ()
) -> str:
    """Prose assembled from facts we hold, never from the model's recollection (§10.4).

    Percentile phrasing only: never a probability of an interview or an offer
    (§8.5, ADR 0006).
    """
    parts: list[str] = []
    if "skill_coverage" in unavailable:
        # Saying "0/0 requirements matched" reads as a failed match rather than
        # a posting we could not parse.
        parts.append("No skill requirements could be extracted from this posting.")
    else:
        parts.append(f"Matches {coverage.fraction} of the listed skill requirements.")
    parts.append(f"Seniority: {fit_reason}.")
    if coverage.missing_must_haves:
        parts.append(f"Missing must-haves: {', '.join(coverage.missing_must_haves[:3])}.")
    parts.append(percentile.describe())
    return " ".join(parts)
