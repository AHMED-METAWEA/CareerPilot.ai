"""Candidate tooling — Phase 4 (§18, §10.3, §10.4).

Gap analysis, tailored bullets, cover letters and interview preparation.

The rule that shapes all four: **facts from the database, prose from the model**
(§10.4). Each generation receives a fact bundle assembled from what the system
actually holds — the candidate's own bullets, the posting's extracted
requirements — and the output is then put through the anti-invention diff. A
generation that asserts anything the CV does not support is regenerated once and
then refused.

Gap analysis and interview preparation need no model at all: both are derived
from the requirements already extracted and the taxonomy already curated. That
is not a limitation to work around — a deterministic gap report is the same
every time a candidate opens it, which is what makes it trustworthy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.llm.base import ChatProvider, LLMError
from app.config import AppConfig
from app.domain.safety.anti_invention import DiffResult, check
from app.domain.safety.taxonomy import SkillTaxonomy
from app.services.taxonomy import load_taxonomy

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = 2
"""One generation, one repair. A model that invents twice on the same input will
invent a third time, and an honest refusal beats a lucky fourth attempt."""


class ToolingError(Exception):
    pass


class GenerationRefused(ToolingError):
    """The generated text asserted something the CV does not support."""

    def __init__(self, result: DiffResult, attempts: int) -> None:
        super().__init__(
            f"Refused after {attempts} attempt(s): {result.summary()}. "
            "Nothing was shown to the candidate."
        )
        self.result = result
        self.attempts = attempts


@dataclass(slots=True)
class FactBundle:
    """Everything a generation is allowed to draw on (§10.4)."""

    candidate_bullets: list[str] = field(default_factory=list)
    candidate_skills: list[str] = field(default_factory=list)
    cv_text: str = ""
    posting_title: str = ""
    company: str | None = None
    posting_text: str = ""
    requirements: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The bundle as the model sees it — facts, labelled, and nothing else."""
        lines = [
            f"ROLE: {self.posting_title}" + (f" at {self.company}" if self.company else ""),
            "",
            "WHAT THE CANDIDATE HAS DONE (from their CV — the only permitted source",
            "for claims about them):",
        ]
        lines += [f"- {bullet}" for bullet in self.candidate_bullets] or ["- (none recorded)"]
        lines += [
            "",
            f"THEIR SKILLS: {', '.join(self.candidate_skills) or '(none recorded)'}",
            "",
            "WHAT THE POSTING ASKS FOR:",
        ]
        lines += [f"- {requirement}" for requirement in self.requirements] or ["- (none extracted)"]
        lines += [
            "",
            f"REQUIREMENTS THEY MEET: {', '.join(self.matched_skills) or 'none identified'}",
            f"REQUIREMENTS THEY DO NOT: {', '.join(self.missing_skills) or 'none identified'}",
        ]
        return "\n".join(lines)


@dataclass(slots=True)
class GapItem:
    skill: str
    is_must_have: bool
    suggestion: str


@dataclass(slots=True)
class GapAnalysis:
    matched: list[str] = field(default_factory=list)
    gaps: list[GapItem] = field(default_factory=list)
    note: str = ""

    @property
    def blocking(self) -> list[GapItem]:
        return [gap for gap in self.gaps if gap.is_must_have]


# Concrete next steps, curated per skill kind rather than generated. A
# suggestion a model invented is a suggestion nobody checked.
_LEARNING_BY_KIND: dict[str, str] = {
    "language": "Build something small end to end in it and put the repository on your CV.",
    "framework": "Port an existing project of yours to it; the contrast is what interviews probe.",
    "tool": "Use it on a project you already have, and be ready to say what broke.",
    "domain": "Take one problem you have solved and redo it in this domain's terms.",
    "soft": "Find an example from your own work that shows it, and write that as a bullet.",
}


class ToolingService:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        llm: ChatProvider | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.llm = llm
        self._taxonomy: SkillTaxonomy | None = None

    @property
    def taxonomy(self) -> SkillTaxonomy:
        if self._taxonomy is None:
            self._taxonomy = load_taxonomy(self.session)
        return self._taxonomy

    # ── gap analysis (deterministic) ─────────────────────────────────

    def gap_analysis(self, user_id: uuid.UUID, match_id: uuid.UUID) -> GapAnalysis:
        """What the posting asks for and the CV does not evidence.

        No model involved: the gaps were computed by the scorer, and reporting
        them differently here would be reporting a different number than the one
        that produced the ranking.
        """
        row = self.session.execute(
            text(
                """
                SELECT m.gaps, m.subscores, p.id AS posting_id
                  FROM matches m
                  JOIN job_postings p ON p.id = m.posting_id
                 WHERE m.id = :id AND m.user_id = :user_id
                """
            ),
            {"id": match_id, "user_id": user_id},
        ).first()
        if row is None:
            raise ToolingError("Match not found")

        must_haves = {
            item.text
            for item in self.session.execute(
                text(
                    """
                    SELECT s.canonical_name AS text FROM job_requirements r
                      JOIN skills s ON s.id = r.skill_id
                     WHERE r.posting_id = :id AND r.is_must_have
                    """
                ),
                {"id": row.posting_id},
            ).all()
        }
        held = set(
            self.session.execute(
                text(
                    """
                    SELECT s.canonical_name FROM profile_skills ps
                      JOIN skills s ON s.id = ps.skill_id
                      JOIN candidate_profiles c ON c.id = ps.profile_id
                     WHERE c.user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .scalars()
            .all()
        )

        analysis = GapAnalysis(matched=sorted(held & must_haves))
        for gap in row.gaps or []:
            # Gaps are stored with their alternatives ("Python (or Java)"); the
            # first name is the one to look up.
            primary = gap.split(" (or ")[0]
            analysis.gaps.append(
                GapItem(
                    skill=gap,
                    is_must_have=primary in must_haves,
                    suggestion=self._suggestion(primary),
                )
            )

        analysis.note = (
            "A gap is a thing to be ready to talk about, not necessarily a reason not to apply. "
            "If you have done it and your CV does not say so, the gap is in the CV."
        )
        return analysis

    def _suggestion(self, skill: str) -> str:
        kind = next(
            (entry.kind.value for entry in self.taxonomy.entries if entry.canonical_name == skill),
            "tool",
        )
        return _LEARNING_BY_KIND.get(kind, _LEARNING_BY_KIND["tool"])

    # ── interview preparation (deterministic) ────────────────────────

    def interview_preparation(self, user_id: uuid.UUID, match_id: uuid.UUID) -> dict[str, Any]:
        """Questions derived from the posting's own extracted requirements (§18).

        Every question cites the requirement it came from, so the candidate can
        see why it is being asked — and no question is asked about a requirement
        the posting did not state.
        """
        row = self.session.execute(
            text(
                """
                SELECT p.id AS posting_id, p.title, c.canonical_name AS company
                  FROM matches m
                  JOIN job_postings p ON p.id = m.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE m.id = :id AND m.user_id = :user_id
                """
            ),
            {"id": match_id, "user_id": user_id},
        ).first()
        if row is None:
            raise ToolingError("Match not found")

        requirements = self.session.execute(
            text(
                """
                SELECT r.text, r.kind, r.is_must_have,
                       (SELECT e.note FROM match_evidence e
                         WHERE e.requirement_id = r.id AND e.match_id = :match_id
                         LIMIT 1) AS evidence,
                       (SELECT e.status FROM match_evidence e
                         WHERE e.requirement_id = r.id AND e.match_id = :match_id
                         LIMIT 1) AS status
                  FROM job_requirements r
                 WHERE r.posting_id = :posting_id
                 ORDER BY r.is_must_have DESC
                """
            ),
            {"posting_id": row.posting_id, "match_id": match_id},
        ).all()

        prepared = []
        for requirement in requirements[:12]:
            prepared.append(
                {
                    "requirement": requirement.text,
                    "must_have": requirement.is_must_have,
                    "likely_question": _question_for(requirement.text, requirement.kind),
                    "your_evidence": requirement.evidence,
                    "status": requirement.status or "unknown",
                    "advice": (
                        "You have something concrete here — lead with it."
                        if requirement.evidence
                        else "Nothing in your CV answers this. Decide now what you will say."
                    ),
                }
            )

        return {
            "role": row.title,
            "company": row.company,
            "questions": prepared,
            "note": (
                "These come from the requirements this posting actually states, not from a "
                "generic list. Nothing here is a prediction about what you will be asked."
            ),
        }

    # ── generation, gated by the diff ────────────────────────────────

    def _require_llm(self) -> ChatProvider:
        if self.llm is None:
            raise ToolingError(
                "Writing needs an inference provider. Gap analysis and interview "
                "preparation work without one."
            )
        return self.llm

    def cover_letter(self, user_id: uuid.UUID, match_id: uuid.UUID) -> dict[str, Any]:
        """A cover letter assembled from a fact bundle and checked afterwards."""
        llm = self._require_llm()
        bundle = self._bundle(user_id, match_id)
        generated = self._generate_checked(
            llm,
            system=COVER_LETTER_SYSTEM_PROMPT,
            user=bundle.render(),
            bundle=bundle,
            component="cover_letter",
        )
        return {"letter": generated, "facts_used": bundle.render()}

    def own_bullets(self, user_id: uuid.UUID, match_id: uuid.UUID) -> list[str]:
        """The candidate's own bullets, which are the only ones offered for rewrite."""
        return self._bundle(user_id, match_id).candidate_bullets

    def rewrite_bullet(
        self, user_id: uuid.UUID, match_id: uuid.UUID, bullet: str
    ) -> dict[str, Any]:
        """Rephrase one of the candidate's own bullets toward this posting.

        The bullet must be one of theirs: rewriting arbitrary text would let a
        caller launder an invented claim through the system.
        """
        llm = self._require_llm()
        bundle = self._bundle(user_id, match_id)
        if bullet not in bundle.candidate_bullets:
            raise ToolingError("That bullet is not from your CV")

        generated = self._generate_checked(
            llm,
            system=BULLET_SYSTEM_PROMPT,
            user=f"{bundle.render()}\n\nREWRITE THIS BULLET, AND ONLY THIS ONE:\n{bullet}",
            bundle=bundle,
            component="bullet_rewrite",
        )
        return {"original": bullet, "rewritten": generated}

    def _generate_checked(
        self, llm: ChatProvider, *, system: str, user: str, bundle: FactBundle, component: str
    ) -> str:
        """Generate, then enforce the anti-invention diff (§10.3)."""
        known_skills = frozenset(
            entry.canonical_name for entry in self.taxonomy.entries
        ) | frozenset(bundle.candidate_skills)
        known_employers = frozenset(
            self.session.execute(text("SELECT canonical_name FROM companies")).scalars().all()
        )

        last: DiffResult | None = None
        prompt = user
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = llm.chat(
                    system=system,
                    user=prompt,
                    model=self.config.models.analysis.model,
                    json_mode=False,
                )
            except LLMError as exc:
                raise ToolingError(f"Generation is unavailable right now: {exc}") from exc

            verdict = check(
                result.content,
                cv=bundle.cv_text,
                context=f"{bundle.posting_title}\n{bundle.posting_text}",
                known_skills=known_skills,
                known_employers=known_employers,
            )
            if verdict.is_clean:
                log.info(
                    "tooling.generated",
                    component=component,
                    attempts=attempt,
                    checked=verdict.checked,
                )
                return result.content.strip()

            last = verdict
            log.warning(
                "tooling.invention_blocked",
                component=component,
                attempt=attempt,
                invented=[str(item) for item in verdict.invented],
            )
            prompt = (
                f"{user}\n\nYour previous draft asserted things the CV does not support: "
                + "; ".join(str(item) for item in verdict.invented)
                + ". Rewrite using only what the facts above state."
            )

        raise GenerationRefused(last or DiffResult(), MAX_ATTEMPTS)

    def _bundle(self, user_id: uuid.UUID, match_id: uuid.UUID) -> FactBundle:
        row = self.session.execute(
            text(
                """
                SELECT m.profile_id, m.gaps, p.id AS posting_id, p.title, p.description_text,
                       c.canonical_name AS company, v.raw_text AS cv_text
                  FROM matches m
                  JOIN job_postings p ON p.id = m.posting_id
                  JOIN candidate_profiles cp ON cp.id = m.profile_id
                  JOIN cv_versions v ON v.id = cp.cv_version_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE m.id = :id AND m.user_id = :user_id
                """
            ),
            {"id": match_id, "user_id": user_id},
        ).first()
        if row is None:
            raise ToolingError("Match not found")

        bullets = (
            self.session.execute(
                text("SELECT text FROM profile_bullets WHERE profile_id = :id ORDER BY ordinal"),
                {"id": row.profile_id},
            )
            .scalars()
            .all()
        )
        skills = (
            self.session.execute(
                text(
                    "SELECT s.canonical_name FROM profile_skills ps "
                    "JOIN skills s ON s.id = ps.skill_id WHERE ps.profile_id = :id"
                ),
                {"id": row.profile_id},
            )
            .scalars()
            .all()
        )
        requirements = (
            self.session.execute(
                text("SELECT text FROM job_requirements WHERE posting_id = :id LIMIT 15"),
                {"id": row.posting_id},
            )
            .scalars()
            .all()
        )

        gaps = [gap.split(" (or ")[0] for gap in (row.gaps or [])]
        return FactBundle(
            candidate_bullets=list(bullets),
            candidate_skills=list(skills),
            cv_text=row.cv_text or "",
            posting_title=row.title,
            company=row.company,
            posting_text=(row.description_text or "")[:4000],
            requirements=list(requirements),
            matched_skills=[skill for skill in skills if skill not in gaps],
            missing_skills=gaps,
        )


def _question_for(requirement: str, kind: str) -> str:
    """Turn a requirement into the question it implies.

    Templated rather than generated: an interview question invented by a model
    is a question nobody checked against the posting.
    """
    trimmed = requirement.rstrip(".").strip()
    if kind == "experience":
        return f"Walk me through a project where you did this: {trimmed}."
    if kind == "education":
        return f"How does your background relate to this: {trimmed}?"
    if kind == "language":
        return f"How comfortable are you working in this language day to day? ({trimmed})"
    if kind == "auth":
        return f"Can you confirm your situation regarding: {trimmed}?"
    return f"Tell me about your experience with: {trimmed}."


COVER_LETTER_SYSTEM_PROMPT = """\
You write a short, plain cover letter using only the facts supplied.

Hard rules — output that breaks any of them is discarded by a check you cannot \
see, so breaking them wastes the attempt:

1. Every claim about the candidate must come from the CV facts above. Do not \
add a skill, an employer, a qualification, a date or a number that is not there.
2. Do not restate a requirement as if the candidate meets it. If they do not \
have something, either say nothing about it or name it honestly as a gap.
3. No invented metrics. If the facts say "four million events", you may say \
that; you may not say "improved performance by 40%".
4. Four short paragraphs at most. No "I am writing to apply for", no "team \
player", no flattery about the company's mission.
5. Plain sentences. The reader has forty of these to get through.

Write only the letter body. No subject line, no addresses, no signature block."""


BULLET_SYSTEM_PROMPT = """\
You rewrite one CV bullet so that it speaks to a specific posting.

Hard rules — output that breaks any of them is discarded:

1. Keep every fact from the original bullet. You may reorder, compress and \
re-emphasise; you may not add.
2. Never add a number, a technology, an employer or a date that is not in the \
original.
3. Lead with the outcome where the original states one.
4. One bullet, one line, no preamble. Return the bullet text only."""
