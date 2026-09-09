"""Schema-constrained CV extraction and its grounding rules (§7.4, §10).

The model is asked for two things per field: the value, and the exact quote from
the CV that justifies it. The code then checks that the quote is really in the
document (`spans.py`) and that any skill resolves to the taxonomy
(`safety/taxonomy.py`). A field that fails either check is discarded rather than
surfaced — the prompt asks nicely, the code decides.

`insufficient_evidence` is a valid answer for every field (§10.5). A model that
declines is more useful than one that guesses, and the abstention rate is worth
watching: a sudden drop usually means the model started inventing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import Seniority
from app.domain.profile.spans import (
    EvidenceSpan,
    VerifiedField,
    verify_field,
)
from app.domain.safety.taxonomy import SkillMatch, SkillTaxonomy

INSUFFICIENT: Final = "insufficient_evidence"
"""A valid answer for every field (§10.5). A model that declines is preferable
to one that guesses, and the abstention rate is monitored as a health metric."""


# ── What the model is asked to return ─────────────────────────────────


class Evidenced(BaseModel):
    """A claim plus the words in the CV that justify it."""

    model_config = ConfigDict(extra="ignore")

    value: str
    quote: str = Field(
        default="",
        description="Verbatim text from the CV supporting this value. Empty if none exists.",
    )


class ExtractedSkill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    quote: str = ""
    years: float | None = None
    proficiency: Literal["beginner", "intermediate", "advanced", "expert"] | None = None


class ExtractedLanguage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    language: str
    cefr: Literal["A1", "A2", "B1", "B2", "C1", "C2", "native"] | None = None
    quote: str = ""


class ExtractedProfile(BaseModel):
    """The schema the extraction model must satisfy (§7.4 task 1)."""

    model_config = ConfigDict(extra="ignore")

    years_experience: float | None = None
    years_experience_quote: str = ""
    seniority_level: Literal[
        "intern", "junior", "mid", "senior", "staff", "principal", "insufficient_evidence"
    ] = INSUFFICIENT
    seniority_quote: str = ""
    locations: list[Evidenced] = Field(default_factory=list)
    work_authorization: list[Evidenced] = Field(
        default_factory=list,
        description="Country and status, e.g. 'EG: citizen', 'EU: requires sponsorship'.",
    )
    languages: list[ExtractedLanguage] = Field(default_factory=list)
    skills: list[ExtractedSkill] = Field(default_factory=list)
    summary: str = ""


# ── What survives verification ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GroundedSkill:
    canonical_name: str
    raw_token: str
    span: EvidenceSpan | None
    years: float | None
    proficiency: str | None
    match: SkillMatch


@dataclass(slots=True)
class GroundedProfile:
    """Only what survived span verification and taxonomy resolution."""

    years_experience: float | None = None
    seniority_level: Seniority | None = None
    locations: list[VerifiedField] = field(default_factory=list)
    work_authorization: list[VerifiedField] = field(default_factory=list)
    languages: list[tuple[str, str | None]] = field(default_factory=list)
    skills: list[GroundedSkill] = field(default_factory=list)
    unmapped_skills: list[SkillMatch] = field(default_factory=list)
    summary: str = ""
    discarded: list[str] = field(default_factory=list)
    """Field names dropped because their quote was not in the CV."""
    fields_checked: int = 0

    @property
    def hallucination_rate(self) -> float:
        return len(self.discarded) / self.fields_checked if self.fields_checked else 0.0

    @property
    def confidence(self) -> float:
        """Share of extracted fields that survived verification."""
        if not self.fields_checked:
            return 0.0
        return round(1.0 - self.hallucination_rate, 2)


def ground_profile(
    source: str, extracted: ExtractedProfile, taxonomy: SkillTaxonomy
) -> GroundedProfile:
    """Verify every extracted field against the CV, and drop what fails.

    This is the mechanism §10.1 describes: not a prompt instruction, a check.
    """
    grounded = GroundedProfile(summary=extracted.summary.strip())
    checked = 0
    discarded: list[str] = []

    if extracted.years_experience is not None:
        checked += 1
        verdict = verify_field(
            source, str(extracted.years_experience), extracted.years_experience_quote
        )
        if verdict.is_grounded:
            grounded.years_experience = extracted.years_experience
        else:
            discarded.append("years_experience")

    if extracted.seniority_level != INSUFFICIENT:
        checked += 1
        verdict = verify_field(source, extracted.seniority_level, extracted.seniority_quote)
        if verdict.is_grounded:
            grounded.seniority_level = Seniority(extracted.seniority_level)
        else:
            discarded.append("seniority_level")

    for location in extracted.locations:
        checked += 1
        verdict = verify_field(source, location.value, location.quote)
        if verdict.is_grounded:
            grounded.locations.append(verdict)
        else:
            discarded.append(f"location:{location.value}")

    for auth in extracted.work_authorization:
        checked += 1
        verdict = verify_field(source, auth.value, auth.quote)
        if verdict.is_grounded:
            grounded.work_authorization.append(verdict)
        else:
            # Work authorisation drives a hard gate; an unverifiable claim here
            # would silently include or exclude entire markets.
            discarded.append(f"work_auth:{auth.value}")

    for language in extracted.languages:
        checked += 1
        verdict = verify_field(source, language.language, language.quote or language.language)
        if verdict.is_grounded:
            grounded.languages.append((language.language, language.cefr))
        else:
            discarded.append(f"language:{language.language}")

    for skill in extracted.skills:
        checked += 1
        verdict = verify_field(source, skill.name, skill.quote or skill.name)
        if not verdict.is_grounded:
            discarded.append(f"skill:{skill.name}")
            continue
        match = taxonomy.resolve(skill.name)
        if match.is_resolved and match.canonical_name:
            grounded.skills.append(
                GroundedSkill(
                    canonical_name=match.canonical_name,
                    raw_token=skill.name,
                    span=verdict.span,
                    years=skill.years,
                    proficiency=skill.proficiency,
                    match=match,
                )
            )
        else:
            # Recorded for taxonomy review, not dropped silently and not
            # promoted into a canonical skill (§10.2).
            grounded.unmapped_skills.append(match)

    grounded.fields_checked = checked
    grounded.discarded = discarded
    return grounded


# ── Prompts ───────────────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured facts from a CV. You are not writing a summary and you \
are not assessing the candidate.

Rules, in order of importance:

1. Every value must be supported by a verbatim quote copied exactly from the CV \
text. The quote is checked against the document programmatically; a value whose \
quote does not appear is discarded.
2. Never infer, round, or fill in. If the CV does not state something, omit it \
or return "insufficient_evidence". Declining is the correct answer more often \
than you expect.
3. Copy skill names as the CV writes them. Do not translate, expand acronyms, \
or add related skills the CV does not mention.
4. Total years of experience must be stated in the CV or computable from dated \
roles it lists. If neither, omit it.
5. The CV may be in Arabic, English, or both. Quote in the language of the CV.

Placeholders such as [[NAME_1]] or [[EMAIL_1]] are redacted personal details. \
Leave them alone; never quote them as evidence.

Return only JSON matching the schema. No commentary."""


def build_extraction_prompt(cv_text: str, *, max_chars: int = 24000) -> str:
    """The user half of the extraction prompt.

    Truncation is explicit rather than silent: a CV long enough to be cut is
    unusual, and the model should know it is seeing part of a document.
    """
    text = cv_text.strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    header = (
        f"CV text follows. It has been truncated to the first {max_chars} characters.\n\n"
        if truncated
        else "CV text follows.\n\n"
    )
    return f"{header}---\n{text}\n---"
