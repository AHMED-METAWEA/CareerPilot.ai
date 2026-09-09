"""Closed-world skill vocabulary (§10.2).

An extracted skill must resolve to a canonical entry through the alias table.
A token that does not resolve is recorded as `unmapped` and queued for review —
it is never silently promoted into a canonical skill, because a taxonomy that
grows itself stops being a taxonomy.

Matching runs in three passes, cheapest first:

1. exact alias (after normalisation) — the curated path;
2. a small set of orthographic rules that a human would not bother to curate
   ("node.js" ≈ "nodejs", "react js" ≈ "react");
3. fuzzy, above a high threshold, and only for tokens long enough for the score
   to mean something.

Cross-lingual matching is the reason this exists at all rather than a set of
strings: "تعلم الآلة" and "machine learning" are the same skill, and only a
curated alias can say so.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz, process

from app.domain.text.arabic import normalize_arabic, segment_scripts

MIN_FUZZY_LENGTH = 5
"""Below this, fuzzy matching is noise: 'go' is one edit from ' go' and 'god'."""


class SkillKind(StrEnum):
    TOOL = "tool"
    LANGUAGE = "language"
    FRAMEWORK = "framework"
    DOMAIN = "domain"
    SOFT = "soft"


class MatchMethod(StrEnum):
    ALIAS = "alias"
    ORTHOGRAPHIC = "orthographic"
    FUZZY = "fuzzy"
    UNMAPPED = "unmapped"


@dataclass(frozen=True, slots=True)
class SkillEntry:
    canonical_name: str
    kind: SkillKind
    aliases: tuple[str, ...] = ()
    esco_uri: str | None = None


@dataclass(frozen=True, slots=True)
class SkillMatch:
    token: str
    canonical_name: str | None
    method: MatchMethod
    confidence: float

    @property
    def is_resolved(self) -> bool:
        return self.canonical_name is not None


_PUNCT = re.compile(r"[^\w\s؀-ۿ+#.]")
_WS = re.compile(r"\s+")
_TRAILING_NOISE = re.compile(
    r"\b(?:experience|expertise|knowledge|skills?|proficiency|advanced|basic|intermediate)\b",
    re.I,
)


def normalize_token(token: str) -> str:
    """Fold a skill token to its comparison form.

    Keeps `+`, `#` and `.` because they carry meaning (`C++`, `C#`, `.NET`,
    `Node.js`), and strips the qualifier words CVs wrap around skills.
    """
    text = unicodedata.normalize("NFKC", token)
    text = normalize_arabic(text)
    # "وPython" is "and Python": the clitic has to come off before the token is
    # looked up, or every skill a conjunction touches resolves as unmapped.
    text = segment_scripts(text)
    text = text.casefold()
    text = _TRAILING_NOISE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip(" .")


def orthographic_variants(token: str) -> set[str]:
    """Variants a human curator would find tedious and a machine finds obvious."""
    base = normalize_token(token)
    variants = {base}
    stripped = base.replace(".", "").replace(" ", "")
    variants.add(stripped)
    variants.add(base.replace(".", " ").strip())
    for suffix in (" js", "js", " framework", " language", " lang"):
        if base.endswith(suffix) and len(base) > len(suffix) + 2:
            variants.add(base[: -len(suffix)].strip())
    if base.endswith("s") and len(base) > 4:
        variants.add(base[:-1])
    return {variant for variant in variants if variant}


class SkillTaxonomy:
    """An index over the curated vocabulary. Built once, queried per token."""

    def __init__(self, entries: list[SkillEntry], *, fuzzy_threshold: float = 0.90) -> None:
        self.entries = entries
        self.fuzzy_threshold = fuzzy_threshold
        self._by_alias: dict[str, str] = {}
        self._by_variant: dict[str, str] = {}

        for entry in entries:
            for alias in (entry.canonical_name, *entry.aliases):
                normalized = normalize_token(alias)
                if normalized:
                    self._by_alias.setdefault(normalized, entry.canonical_name)
                for variant in orthographic_variants(alias):
                    self._by_variant.setdefault(variant, entry.canonical_name)

        self._alias_keys = list(self._by_alias)
        # Longest first, so "apache kafka" is preferred over "kafka".
        self._aliases_by_length = sorted(self._by_alias, key=len, reverse=True)

    def resolve(self, token: str) -> SkillMatch:
        normalized = normalize_token(token)
        if not normalized:
            return SkillMatch(token, None, MatchMethod.UNMAPPED, 0.0)

        canonical = self._by_alias.get(normalized)
        if canonical:
            return SkillMatch(token, canonical, MatchMethod.ALIAS, 1.0)

        for variant in orthographic_variants(token):
            canonical = self._by_variant.get(variant)
            if canonical:
                return SkillMatch(token, canonical, MatchMethod.ORTHOGRAPHIC, 0.95)

        if len(normalized) >= MIN_FUZZY_LENGTH and self._alias_keys:
            best = process.extractOne(normalized, self._alias_keys, scorer=fuzz.WRatio)
            if best and best[1] / 100.0 >= self.fuzzy_threshold:
                return SkillMatch(
                    token, self._by_alias[best[0]], MatchMethod.FUZZY, best[1] / 100.0
                )

        # Honest outcome: recorded for review, never invented into the taxonomy.
        return SkillMatch(token, None, MatchMethod.UNMAPPED, 0.0)

    def find_in_text(self, text: str, *, limit: int = 8) -> list[SkillMatch]:
        """Find known skills mentioned anywhere in a sentence.

        A job requirement names its skills in prose — "Strong experience with
        Apache Kafka and streaming systems" — so resolving the whole sentence as
        one token finds nothing. This scans for known aliases instead, which is
        what makes skill coverage computable without a language model, and is
        therefore what the ablation baseline rests on.

        Longest aliases win: "Apache Kafka" is matched in preference to "Kafka",
        so a requirement resolves to one skill rather than two.
        """
        haystack = f" {normalize_token(text)} "
        if not haystack.strip():
            return []

        found: dict[str, SkillMatch] = {}
        consumed: list[tuple[int, int]] = []
        for alias in self._aliases_by_length:
            if len(alias) < 2:
                continue
            position = haystack.find(f" {alias} ")
            if position < 0:
                continue
            start, end = position, position + len(alias) + 2
            # A longer alias already covering this span wins.
            if any(start >= s and end <= e for s, e in consumed):
                continue
            consumed.append((start, end))
            canonical = self._by_alias[alias]
            found.setdefault(canonical, SkillMatch(alias, canonical, MatchMethod.ALIAS, 1.0))
            if len(found) >= limit:
                break
        return list(found.values())

    def resolve_all(self, tokens: list[str]) -> tuple[list[SkillMatch], list[SkillMatch]]:
        """Returns (resolved, unmapped), preserving input order within each."""
        matches = [self.resolve(token) for token in tokens]
        return (
            [match for match in matches if match.is_resolved],
            [match for match in matches if not match.is_resolved],
        )

    def __len__(self) -> int:
        return len(self.entries)
