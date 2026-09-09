"""CV parse-quality scoring (§11.1 step 4).

The premise, from §2.4: real ATS failures are parsing failures, not keyword
density. A CV laid out in two columns, or delivered as a scanned image, loses
information before any model sees it — and the honest response is to tell the
candidate that, not to extract confidently from mush.

The score is a blend of four signals, each independently reportable so the
candidate gets an actionable report rather than a number:

* **character yield** — text extracted per page, against what a normal CV holds
* **section detection** — whether the standard headings survived extraction
* **layout damage** — column bleed and table artefacts, which scramble reading
  order and are invisible in the extracted text unless you look for them
* **contact block** — whether an email or phone survived, a cheap proxy for the
  header having been read at all

Below `MIN_QUALITY` the pipeline stops and surfaces the report (§11.1 step 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

MIN_QUALITY = 0.60
"""Below this, extraction is not attempted. §11.1 step 5."""

# A one-page CV that extracted cleanly yields roughly 1,800–3,500 characters.
# Well under that means the text layer is thin: a scan, an image, or a PDF whose
# fonts did not map.
CHARS_PER_PAGE_GOOD = 1500
CHARS_PER_PAGE_POOR = 400

SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "experience": re.compile(
        r"\b(work\s+)?experience\b|\bemployment\b|\bcareer\s+history\b|الخبرة|الخبرات|الخبره",
        re.I,
    ),
    "education": re.compile(
        r"\beducation\b|\bacademic\b|\bqualifications\b|التعليم|المؤهلات", re.I
    ),
    "skills": re.compile(r"\bskills\b|\btechnical\s+skills\b|\bcompetenc", re.I),
    "contact": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s()-]{7,}\d"),
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s()-]{7,}\d")
# A wide run of spaces with text on both sides: the signature of a two-column
# layout flattened into text, where the gutter survives as whitespace and words
# from both columns land on one line. One gap per line is what this actually
# looks like — requiring several per line detects nothing.
_COLUMN_BLEED = re.compile(r"\S {3,}\S")
_BULLET = re.compile(r"^\s*[•▪◦\-*·]\s+", re.M)


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    message: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class ParseabilityReport:
    """What the candidate is shown, and what gates extraction."""

    quality: float
    character_yield: float
    sections_found: tuple[str, ...]
    sections_missing: tuple[str, ...]
    layout_damage: float
    has_contact: bool
    page_count: int
    character_count: int
    findings: tuple[Finding, ...] = field(default=())

    @property
    def is_processable(self) -> bool:
        return self.quality >= MIN_QUALITY


def score_parseability(text: str, *, page_count: int = 1) -> ParseabilityReport:
    """Score an extracted CV and explain the score.

    Deliberately not a model: a candidate who is told their CV is unreadable
    deserves a reason they can act on, and every one of these signals maps to a
    concrete fix.
    """
    page_count = max(page_count, 1)
    characters = len(text.strip())
    per_page = characters / page_count

    yield_score = _clamp(
        (per_page - CHARS_PER_PAGE_POOR) / (CHARS_PER_PAGE_GOOD - CHARS_PER_PAGE_POOR)
    )

    found = tuple(name for name, pattern in SECTION_PATTERNS.items() if pattern.search(text))
    missing = tuple(name for name in SECTION_PATTERNS if name not in found)
    section_score = len(found) / len(SECTION_PATTERNS)

    damage = _layout_damage(text)
    layout_score = 1.0 - damage

    has_contact = bool(_EMAIL.search(text) or _PHONE.search(text))

    quality = _clamp(
        0.40 * yield_score
        + 0.30 * section_score
        + 0.20 * layout_score
        + 0.10 * (1.0 if has_contact else 0.0)
    )

    return ParseabilityReport(
        quality=round(quality, 2),
        character_yield=round(per_page, 1),
        sections_found=found,
        sections_missing=missing,
        layout_damage=round(damage, 2),
        has_contact=has_contact,
        page_count=page_count,
        character_count=characters,
        findings=_findings(per_page, missing, damage, has_contact, text),
    )


def _layout_damage(text: str) -> float:
    """Share of lines showing column bleed or table artefacts.

    Multi-column CVs extract as interleaved fragments: the reading order is
    destroyed, which no amount of downstream cleverness recovers.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 1.0
    damaged = sum(1 for line in lines if _COLUMN_BLEED.search(line) or line.count("\t") > 1)
    return _clamp(damaged / len(lines))


def _findings(
    per_page: float,
    missing: tuple[str, ...],
    damage: float,
    has_contact: bool,
    text: str,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    if per_page < CHARS_PER_PAGE_POOR:
        findings.append(
            Finding(
                code="low_text_yield",
                severity=Severity.BLOCKER,
                message=f"Only {per_page:.0f} characters of text per page were recoverable.",
                suggestion=(
                    "This usually means the CV is a scan or an image. Export a text-based "
                    "PDF from your word processor rather than printing and scanning."
                ),
            )
        )
    elif per_page < CHARS_PER_PAGE_GOOD:
        findings.append(
            Finding(
                code="thin_text_yield",
                severity=Severity.WARNING,
                message=f"{per_page:.0f} characters per page is thinner than a typical CV.",
                suggestion="Check that text in graphics or text boxes is real text, not an image.",
            )
        )

    if damage > 0.25:
        findings.append(
            Finding(
                code="multi_column_layout",
                severity=Severity.BLOCKER if damage > 0.5 else Severity.WARNING,
                message=f"{damage:.0%} of lines show column or table bleed.",
                suggestion=(
                    "Multi-column layouts interleave when extracted, which scrambles reading "
                    "order. A single-column layout parses reliably everywhere."
                ),
            )
        )

    for section in missing:
        if section == "contact":
            continue
        findings.append(
            Finding(
                code=f"missing_section_{section}",
                severity=Severity.WARNING,
                message=f"No recognisable '{section}' heading was found.",
                suggestion=(
                    f"Add a plain '{section.title()}' heading. Stylised or image-based headings "
                    "are invisible to every parser, not just this one."
                ),
            )
        )

    if not has_contact:
        findings.append(
            Finding(
                code="no_contact_details",
                severity=Severity.WARNING,
                message="No email address or phone number was recovered.",
                suggestion=(
                    "Contact details in a header or footer are frequently dropped in "
                    "extraction. Put them in the body of the first page."
                ),
            )
        )

    if not _BULLET.search(text) and len(text) > 800:
        findings.append(
            Finding(
                code="no_bullet_structure",
                severity=Severity.INFO,
                message="No bullet structure was detected.",
                suggestion=(
                    "Bullets give per-achievement evidence spans, which makes the match "
                    "explanation more specific."
                ),
            )
        )

    return tuple(findings)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
