"""Segmenting a CV into evidence-bearing bullets (§7.2).

Requirement alignment compares each job requirement against each CV bullet, so
the quality of that comparison is bounded by how well the CV is cut up. Two
things matter and are easy to get wrong:

* **A bullet must carry its span.** The bullet that matched a requirement is
  shown to the user as the evidence, highlighted in their own CV, so its
  character offsets have to survive segmentation.
* **A wrapped line is not a new bullet.** PDF extraction breaks long bullets
  across lines; treating each fragment as a separate bullet halves the context
  the similarity ever sees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.profile.spans import EvidenceSpan

BULLET_MARKER = re.compile(r"^[\s]*[•▪◦●·*\-–—]\s+")
SECTION_HEADING = re.compile(
    r"^\s*(experience|employment|work history|education|skills|projects|certifications|"
    r"summary|profile|about|languages|الخبرة|الخبرات|التعليم|المهارات|المشاريع|اللغات)"
    r"\s*:?\s*$",
    re.I,
)
_SENTENCE_END = re.compile(r"[.!?؟]\s*$")

MIN_BULLET_CHARS = 25
"""Shorter fragments are headings, dates or artefacts, not evidence."""
MAX_BULLET_CHARS = 600


@dataclass(frozen=True, slots=True)
class Bullet:
    text: str
    span: EvidenceSpan
    section: str | None = None
    ordinal: int = 0


def extract_bullets(text: str) -> list[Bullet]:
    """Split a CV into bullets, keeping offsets into the original text.

    Continuation lines are folded into the preceding bullet; a line only starts
    a new bullet if it carries a marker, or the previous bullet already looks
    finished.
    """
    bullets: list[Bullet] = []
    section: str | None = None

    current_start: int | None = None
    current_end = 0
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_parts, current_end
        if current_start is None:
            return
        joined = " ".join(part.strip() for part in current_parts).strip()
        if len(joined) >= MIN_BULLET_CHARS:
            bullets.append(
                Bullet(
                    text=joined[:MAX_BULLET_CHARS],
                    span=EvidenceSpan(current_start, current_end),
                    section=section,
                    ordinal=len(bullets),
                )
            )
        current_start, current_parts, current_end = None, [], 0

    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        line_start = offset
        line_end = offset + len(line.rstrip("\n"))
        offset += len(line)

        if not stripped:
            flush()
            continue

        heading = SECTION_HEADING.match(stripped)
        if heading:
            flush()
            section = heading.group(1).casefold()
            continue

        marker = BULLET_MARKER.match(line)
        starts_new = (
            marker is not None
            or current_start is None
            or (current_parts and _SENTENCE_END.search(current_parts[-1]) is not None)
        )

        if starts_new:
            flush()
            # The span starts at the content, not at the marker: highlighting a
            # bullet in the candidate's CV should not include the bullet glyph.
            content_offset = marker.end() if marker else len(line) - len(line.lstrip())
            current_start = line_start + content_offset
            current_parts = [line[content_offset:].strip()]
        else:
            # A wrapped continuation of the previous bullet.
            current_parts.append(stripped)
        current_end = line_end

    flush()
    return bullets


def verify_bullet_spans(text: str, bullets: list[Bullet]) -> list[Bullet]:
    """Keep only bullets whose span still recovers their text.

    Cheap insurance: an off-by-one in segmentation would highlight the wrong
    line in the candidate's CV, which reads as the system making things up.
    """
    kept: list[Bullet] = []
    for bullet in bullets:
        recovered = " ".join(bullet.span.slice(text).split())
        expected = " ".join(bullet.text.split())
        if recovered.startswith(expected[: min(len(expected), 40)]):
            kept.append(bullet)
    return kept
