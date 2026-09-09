"""Evidence spans: extraction is not generation (§10.1).

Every extracted field carries character offsets into its source document, and
this module verifies that the cited substring actually exists at the cited
offsets. Fields failing the check are discarded, not surfaced.

That single mechanism removes the majority of fabrication without relying on
prompt instructions — a model that invents "Kubernetes" cannot also invent
offsets where the word appears. The prompt asks for the quote; the code decides
whether it was real.

Verification is tolerant of whitespace and case, because a model re-typing a
quote normalises spacing, and intolerant of everything else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_WS = re.compile(r"\s+")


class SpanStatus(StrEnum):
    EXACT = "exact"
    """The quote sits at the offsets the model gave."""
    RELOCATED = "relocated"
    """The quote is in the document, but not where the model said. Offsets corrected."""
    NOT_FOUND = "not_found"
    """The quote is not in the document. The field is discarded."""


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid span [{self.start}, {self.end})")

    def slice(self, text: str) -> str:
        return text[self.start : self.end]

    def as_int4range(self) -> str:
        """Postgres literal for the `int4range` columns in §6.2."""
        return f"[{self.start},{self.end})"


@dataclass(frozen=True, slots=True)
class VerifiedField:
    value: str
    quote: str
    span: EvidenceSpan | None
    status: SpanStatus

    @property
    def is_grounded(self) -> bool:
        return self.status is not SpanStatus.NOT_FOUND


def normalize_for_comparison(text: str) -> str:
    """Casefold, fold Unicode, collapse whitespace.

    The tolerance is deliberate and bounded: a model that re-types a quote will
    change spacing and case. Anything beyond that is a different string.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WS.sub(" ", folded).strip()


def verify_span(source: str, span: EvidenceSpan, quote: str) -> SpanStatus:
    """Does `quote` sit at `span` in `source`?"""
    if not quote.strip():
        return SpanStatus.NOT_FOUND
    if span.end > len(source):
        return _relocatable(source, quote)
    if normalize_for_comparison(span.slice(source)) == normalize_for_comparison(quote):
        return SpanStatus.EXACT
    return _relocatable(source, quote)


def _relocatable(source: str, quote: str) -> SpanStatus:
    return SpanStatus.RELOCATED if find_span(source, quote) else SpanStatus.NOT_FOUND


def find_span(source: str, quote: str) -> EvidenceSpan | None:
    """Locate `quote` in `source`, tolerating whitespace and case differences.

    Returns offsets into the *original* text, so the span still highlights
    correctly in the document the candidate uploaded.
    """
    needle = quote.strip()
    if not needle:
        return None

    index = source.find(needle)
    if index >= 0:
        return EvidenceSpan(index, index + len(needle))

    # Fall back to a whitespace- and case-insensitive search, mapping the match
    # back to original offsets through an index built during normalisation.
    normalized, offsets = _normalize_with_offsets(source)
    target = normalize_for_comparison(needle)
    if not target:
        return None
    position = normalized.find(target)
    if position < 0:
        return None
    start = offsets[position]
    end_index = position + len(target) - 1
    end = offsets[end_index] + 1 if end_index < len(offsets) else len(source)
    return EvidenceSpan(start, end)


def _normalize_with_offsets(source: str) -> tuple[str, list[int]]:
    """Normalised text plus, per normalised character, its original index."""
    chars: list[str] = []
    offsets: list[int] = []
    previous_space = True  # suppresses leading whitespace
    for index, char in enumerate(unicodedata.normalize("NFKC", source).casefold()):
        if char.isspace():
            if previous_space:
                continue
            chars.append(" ")
            offsets.append(index)
            previous_space = True
        else:
            chars.append(char)
            offsets.append(index)
            previous_space = False
    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


def verify_field(
    source: str, value: str, quote: str, span: EvidenceSpan | None = None
) -> VerifiedField:
    """Verify one extracted field against its source document."""
    if span is not None:
        status = verify_span(source, span, quote)
        if status is SpanStatus.EXACT:
            return VerifiedField(value=value, quote=quote, span=span, status=status)
    else:
        status = _relocatable(source, quote)

    located = find_span(source, quote)
    if located is None:
        return VerifiedField(value=value, quote=quote, span=None, status=SpanStatus.NOT_FOUND)
    return VerifiedField(value=value, quote=quote, span=located, status=SpanStatus.RELOCATED)


def hallucination_rate(fields: list[VerifiedField]) -> float:
    """Share of extracted fields whose quote is not in the source (§9.2, §10.6).

    Recorded per extraction run in `model_runs.metrics` and tracked over time;
    the published target is under 2%.
    """
    if not fields:
        return 0.0
    return sum(1 for field in fields if not field.is_grounded) / len(fields)
