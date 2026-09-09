"""Requirement extraction from a job description (§7.4 task 1, §11.4 stage 5).

Two paths, deliberately:

* **Heuristic** — bullet segmentation plus must-have language. Deterministic,
  free, and available before any inference key exists. It is also the baseline
  the ablation in §9.3 measures the LLM path against, which is the only way to
  know whether the model is earning its tokens.
* **Model** — schema-constrained extraction on the last twenty-five postings,
  where the funnel has already made the cost affordable.

Both produce the same shape, and both carry spans into the description, so a
requirement can be shown in context whichever path produced it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import PostingRequirement
from app.domain.profile.bullets import BULLET_MARKER
from app.domain.profile.spans import EvidenceSpan, find_span

MUST_HAVE = re.compile(
    r"\b(?:must[- ]have|required|requirement|essential|minimum|at least|proven|"
    r"strong (?:experience|background)|you (?:have|will need)|we require|مطلوب|يشترط)\b",
    re.I,
)
NICE_TO_HAVE = re.compile(
    r"\b(?:nice[- ]to[- ]have|preferred|plus|bonus|desirable|advantage|ideally|"
    r"would be great|يفضل)\b",
    re.I,
)
YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?|سنوات|سنة)\b", re.I)
EDUCATION = re.compile(
    r"\b(?:bachelor|master|phd|degree|bsc|msc|b\.s\.|m\.s\.|university|بكالوريوس|ماجستير)\b", re.I
)
LANGUAGE_REQ = re.compile(
    r"\b(?:fluent|fluency|native|proficient|proficiency)\b.{0,30}\b"
    r"(english|arabic|french|german|spanish|الإنجليزية|العربية)\b",
    re.I,
)
AUTHORIZATION = re.compile(
    r"\b(?:work (?:permit|authorization|authorisation)|visa|eligible to work|right to work|"
    r"sponsorship|تصريح عمل)\b",
    re.I,
)
SPONSORSHIP_OFFERED = re.compile(
    r"\b(?:visa sponsorship (?:is )?(?:available|provided|offered)|we sponsor|"
    r"sponsorship (?:is )?available|relocation (?:package|support) (?:is )?(?:available|provided))\b",
    re.I,
)
SPONSORSHIP_REFUSED = re.compile(
    r"\b(?:no (?:visa )?sponsorship|unable to sponsor|cannot sponsor|"
    r"sponsorship is not (?:available|provided)|must be authoriz?sed to work)\b",
    re.I,
)

SECTION_HEADING = re.compile(
    r"^\s*(requirements?|qualifications?|what you.{0,12}(?:bring|need|have)|"
    r"who you are|about you|skills?|must have|nice to have|responsibilities|المؤهلات|المتطلبات)"
    r"\s*:?\s*$",
    re.I,
)

MIN_REQUIREMENT_CHARS = 15
MAX_REQUIREMENT_CHARS = 400
MAX_REQUIREMENTS = 30
"""A posting listing more than this is listing responsibilities, not requirements."""


class LLMRequirement(BaseModel):
    """One requirement as the extraction model returns it."""

    model_config = ConfigDict(extra="ignore")

    text: str
    quote: str = Field(default="", description="Verbatim text from the job description.")
    kind: str = "skill"
    is_must_have: bool = False
    skill: str | None = None


class ExtractedRequirements(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requirements: list[LLMRequirement] = Field(default_factory=list)
    min_years: float | None = None
    requires_authorization_in: list[str] = Field(default_factory=list)
    offers_sponsorship: bool | None = None


def classify_kind(text_value: str) -> str:
    """Which gate or sub-score a requirement feeds."""
    if AUTHORIZATION.search(text_value):
        return "auth"
    if LANGUAGE_REQ.search(text_value):
        return "language"
    if EDUCATION.search(text_value):
        return "education"
    if YEARS.search(text_value):
        return "experience"
    return "skill"


def extract_requirements_heuristic(
    description: str,
) -> list[tuple[PostingRequirement, EvidenceSpan | None]]:
    """Split a description into requirement-shaped lines, deterministically.

    Section context carries: a bullet under "Nice to have" is a nice-to-have
    even when its own wording sounds mandatory, which is how job ads are
    actually written.
    """
    results: list[tuple[PostingRequirement, EvidenceSpan | None]] = []
    section_must_have: bool | None = None
    section_has_bullets = False
    offset = 0

    for line in description.splitlines(keepends=True):
        raw = line
        stripped = line.strip()
        line_start = offset
        offset += len(raw)

        if not stripped:
            continue

        heading = SECTION_HEADING.match(stripped)
        if heading:
            label = heading.group(1).casefold()
            if "nice" in label or "preferred" in label:
                section_must_have = False
            elif "responsibilit" in label:
                section_must_have = None  # responsibilities are not requirements
            else:
                section_must_have = True
            section_has_bullets = False
            continue

        marker = BULLET_MARKER.match(raw)
        if marker is None and section_must_have is None:
            continue  # prose outside a requirements section
        if marker is None and section_has_bullets:
            # Once a section is a bullet list, trailing prose is commentary —
            # "We are unable to sponsor visas" is a fact about the employer, not
            # a requirement the candidate can meet.
            continue
        if marker is not None:
            section_has_bullets = True

        content_start = marker.end() if marker else len(raw) - len(raw.lstrip())
        content = raw[content_start:].strip()
        if not (MIN_REQUIREMENT_CHARS <= len(content) <= MAX_REQUIREMENT_CHARS):
            continue

        must_have = bool(MUST_HAVE.search(content))
        if NICE_TO_HAVE.search(content):
            must_have = False
        elif not must_have and section_must_have is not None:
            must_have = section_must_have

        results.append(
            (
                PostingRequirement(
                    text=content, kind=classify_kind(content), is_must_have=must_have
                ),
                EvidenceSpan(line_start + content_start, line_start + content_start + len(content)),
            )
        )
        if len(results) >= MAX_REQUIREMENTS:
            break

    return results


def stated_min_years(description: str) -> float | None:
    """The smallest explicitly stated years figure, or None.

    The *smallest* on purpose: a posting saying "3+ years, ideally 5" has a
    floor of three, and the seniority gate must not fire on the aspiration.
    """
    figures = [float(match.group(1)) for match in YEARS.finditer(description)]
    plausible = [value for value in figures if 0 < value <= 25]
    return min(plausible) if plausible else None


def sponsorship_stance(description: str) -> bool | None:
    """True if sponsorship is offered, False if refused, None if unstated.

    Unstated is the common case and must stay distinct from refused: gating on
    silence would hide most of the corpus from candidates who need a visa.
    """
    if SPONSORSHIP_REFUSED.search(description):
        return False
    if SPONSORSHIP_OFFERED.search(description):
        return True
    return None


REQUIREMENTS_SYSTEM_PROMPT = """\
You extract hiring requirements from a job description.

Rules:
1. Each requirement must be supported by a verbatim quote from the description. \
The quote is checked programmatically; unquoted requirements are discarded.
2. Extract requirements, not responsibilities. "You will own our billing \
service" is a responsibility. "5+ years with distributed systems" is a \
requirement.
3. Mark is_must_have true only where the description makes it mandatory, or it \
sits under a requirements heading. Anything under "nice to have" is false.
4. `skill` must be the tool, language or framework as the description names it. \
Leave it null for requirements that are not about a named skill.
5. min_years is the lowest explicitly stated figure, or null. Never infer it \
from seniority in the title.
6. Do not invent, translate or generalise.

Return only JSON matching the schema."""


def build_requirements_prompt(title: str, description: str, *, max_chars: int = 6000) -> str:
    return f"Job title: {title}\n\nDescription:\n---\n{description[:max_chars]}\n---"


def ground_requirements(
    description: str, extracted: ExtractedRequirements
) -> list[tuple[PostingRequirement, EvidenceSpan | None]]:
    """Keep only model-extracted requirements whose quote is in the description."""
    grounded: list[tuple[PostingRequirement, EvidenceSpan | None]] = []
    for item in extracted.requirements:
        quote = item.quote or item.text
        span = find_span(description, quote)
        if span is None:
            continue
        grounded.append(
            (
                PostingRequirement(
                    text=item.text[:MAX_REQUIREMENT_CHARS],
                    kind=item.kind
                    if item.kind in {"skill", "experience", "education", "auth", "language"}
                    else "skill",
                    is_must_have=item.is_must_have,
                    skill=item.skill,
                ),
                span,
            )
        )
    return grounded[:MAX_REQUIREMENTS]


def expand_skill_requirements(
    requirements: Sequence[PostingRequirement], find_skills: Callable[[str], list[str]]
) -> list[PostingRequirement]:
    """One requirement per named skill, for coverage scoring.

    Sentence-level requirements are the right unit for evidence — the user sees
    the sentence and the CV bullet that answered it — but the wrong unit for
    coverage. "Comfortable with SQL and PostgreSQL" is two skills to hold, and
    counting it as one would let a candidate with half of it score full marks.

    Requirements naming no known skill are dropped here rather than counted as
    unmet: an unresolvable requirement is a gap in the taxonomy, not a gap in
    the candidate (§10.2).
    """
    expanded: list[PostingRequirement] = []
    seen: set[tuple[str, bool]] = set()
    for requirement in requirements:
        if requirement.kind not in {"skill", "experience"}:
            continue
        names = [requirement.skill] if requirement.skill else find_skills(requirement.text)
        for name in names:
            if not name or (name, requirement.is_must_have) in seen:
                continue
            seen.add((name, requirement.is_must_have))
            expanded.append(
                PostingRequirement(
                    text=requirement.text,
                    kind="skill",
                    is_must_have=requirement.is_must_have,
                    skill=name,
                )
            )
    return expanded
