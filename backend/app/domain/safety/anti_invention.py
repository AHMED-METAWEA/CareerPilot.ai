"""The anti-invention diff (§10.3).

Generated text — a rewritten bullet, a cover letter, interview notes — is
tokenised, and every skill, employer, credential, date and figure in it is
checked against the source material. Anything the source does not support fails
the response and triggers regeneration.

This is what converts "never invent skills or experience" from a prompt
instruction into a mechanical guarantee. A prompt asks; this decides.

Four design points, each of which the adversarial set found the hard way:

* **Only claim-bearing tokens are checked.** Connective prose ("delivered",
  "which allowed the team to") is the model's job, not an assertion about the
  candidate. Checking every word would fail every generation and teach whoever
  ships it to disable the check.
* **Sources are typed, not pooled.** The CV is what the candidate *has*; the
  posting is what the employer *wants*. A cover letter may name the role and the
  company it is addressed to, and may discuss a requirement — but "my Kubernetes
  experience" is an invented skill even when the posting asks for Kubernetes,
  because the posting is not evidence about the candidate.
* **Figures are compared as value *and* unit.** "40%" is not supported by "forty
  minutes". Matching on the number alone let an invented percentage through
  because an unrelated duration in the CV happened to share its value.
* **Any number with a unit is a claim.** Enumerating units ("users", "requests")
  misses "30 engineers" and "15 projects". A number followed by a noun is
  checked; a number that appears nowhere in the CV is invented.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.text.arabic import normalize_for_matching


class Violation(StrEnum):
    SKILL = "skill"
    EMPLOYER = "employer"
    CREDENTIAL = "credential"
    DATE = "date"
    METRIC = "metric"


# Credentials someone either holds or does not.
CREDENTIAL_PATTERN = re.compile(
    r"\b(?:PhD|MSc|BSc|MBA|BEng|MEng|B\.?A\.?|M\.?A\.?|"
    r"AWS Certified[\w\s]*|Azure(?:\s+\w+)?\s+Certified|CKA|CKAD|CISSP|PMP|CFA|"
    r"Google Cloud Certified|Professional Cloud[\w\s]*|Scrum Master|Six Sigma)\b",
    re.I,
)
DATE_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")

_NUMBER_WORDS: dict[str, float] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "hundred": 100,
}
_MAGNITUDES: dict[str, int] = {"k": 3, "thousand": 3, "million": 6, "billion": 9}

# A figure: a number (digits or words), an optional magnitude, and the thing it
# counts. The unit is captured because a value alone means nothing — "40" is a
# percentage, a headcount or a duration depending on what follows it.
FIGURE_PATTERN = re.compile(
    # `\d[\d,]*(?:\.\d+)?` rather than `\d[\d,.]*`: the greedy version
    # swallowed the sentence's full stop, so "2021." was five characters long
    # and stopped looking like a year.
    r"(?P<number>\d[\d,]*(?:\.\d+)?|\b(?:" + "|".join(_NUMBER_WORDS) + r")\b)"
    r"\s*(?P<magnitude>k|thousand|million|billion)?"
    r"\s*(?P<unit>%|percent|x\b|[A-Za-z][\w-]*)?",
    re.I,
)

# Figures are compared by value and *category*, not by the exact noun. A CV
# saying "four million crash events" and a letter saying "4,000,000 events" make
# the same claim; "40%" and "forty minutes" do not, even though both are 40.
_DURATION_UNITS = frozenset(
    {"year", "month", "week", "day", "hour", "minute", "second", "yr", "mo", "hr", "min"}
)


def _category(unit: str) -> str:
    normalized = _UNIT_ALIASES.get(unit, unit)
    if normalized == "percent":
        return "percent"
    if normalized == "multiple":
        return "multiple"
    if normalized in _DURATION_UNITS:
        return "duration"
    return "count"


# Phrases that turn a mention into a disclaimer. Naming a skill you do not have
# is the honest thing a cover letter can do about a gap (§2.3), and a guard that
# blocked it would push generated text toward saying nothing about gaps at all.
NEGATION = re.compile(
    r"\b(?:not|never|no|without|lack|lacks|lacking|little|limited|unfamiliar|"
    r"yet to|haven.t|hasn.t|don.t|doesn.t|outside)\b",
    re.I,
)
NEGATION_WINDOW = 60

# Units that mean the same thing, so a rewording is not an invention.
_UNIT_ALIASES: dict[str, str] = {
    "%": "percent",
    "percent": "percent",
    "pct": "percent",
    "x": "multiple",
    "times": "multiple",
    "yr": "year",
    "yrs": "year",
    "year": "year",
    "years": "year",
    "month": "month",
    "months": "month",
    "mo": "month",
    "week": "week",
    "weeks": "week",
    "day": "day",
    "days": "day",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "hrs": "hour",
    "minute": "minute",
    "minutes": "minute",
    "min": "minute",
    "mins": "minute",
}

_WORD = re.compile(r"[\w؀-ۿ][\w؀-ۿ+#.\-]*")


@dataclass(frozen=True, slots=True)
class InventedToken:
    kind: Violation
    text: str
    context: str

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.text!r}"


@dataclass(slots=True)
class DiffResult:
    """The verdict on one piece of generated text."""

    invented: list[InventedToken] = field(default_factory=list)
    checked: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.invented

    def summary(self) -> str:
        if self.is_clean:
            return f"{self.checked} claim-bearing tokens, all traceable to the source"
        kinds = ", ".join(sorted({item.kind.value for item in self.invented}))
        return f"{len(self.invented)} invented token(s) [{kinds}] out of {self.checked} checked"


def normalize_for_match(text: str) -> str:
    """Fold both sides of every comparison identically (§Phase 5).

    Script segmentation is part of the fold: Arabic attaches its conjunction to
    the following word with no space, so a CV reading "خبرة في Python وKafka"
    contains `Kafka` only after the boundary is opened. Without it the diff
    reads Kafka as absent from the CV and refuses a letter that was telling the
    truth — the worst failure this component has, because it is invisible and
    it punishes honesty.
    """
    return normalize_for_matching(text)


def _mentions(haystack_normalized: str, term: str) -> bool:
    """Whole-term match, so `Go` is not found inside `Google`.

    Skill names carry regex-significant characters (`C++`, `.NET`, `Node.js`),
    so the term is escaped and its boundaries asserted by lookaround — `\\b`
    does not behave usefully next to punctuation.
    """
    needle = normalize_for_match(term).strip()
    if not needle:
        return False
    return re.search(rf"(?<![\w+#.]){re.escape(needle)}(?![\w+#])", haystack_normalized) is not None


def check(
    generated: str,
    *,
    cv: str,
    context: str = "",
    known_skills: frozenset[str] = frozenset(),
    known_employers: frozenset[str] = frozenset(),
) -> DiffResult:
    """Fail `generated` if it asserts anything the candidate's CV does not support.

    `cv` is the evidence about the candidate. `context` is material the text may
    legitimately refer to but which says nothing about them — normally the job
    description, which supplies the company name and the role title.
    """
    evidence = normalize_for_match(cv)
    referable = normalize_for_match(f"{cv}\n{context}")
    result = DiffResult()
    generated_normalized = normalize_for_match(generated)

    def record(kind: Violation, token: str) -> None:
        result.invented.append(
            InventedToken(kind=kind, text=token, context=_context(generated, token))
        )

    # Skills are claims about the candidate, so only the CV can support them.
    # A posting asking for Kubernetes is not evidence that they have used it.
    for skill in known_skills:
        if _mentions(generated_normalized, skill):
            if _is_disclaimed(generated, skill):
                continue  # "which I have not used" is a gap, not a claim
            result.checked += 1
            if not _mentions(evidence, skill):
                record(Violation.SKILL, skill)

    # Employers may come from either: the letter is addressed to one of them.
    for employer in known_employers:
        if _mentions(generated_normalized, employer):
            result.checked += 1
            if not _mentions(referable, employer):
                record(Violation.EMPLOYER, employer)

    for match in CREDENTIAL_PATTERN.finditer(generated):
        result.checked += 1
        if not _mentions(evidence, match.group(0)):
            record(Violation.CREDENTIAL, match.group(0))

    for match in DATE_PATTERN.finditer(generated):
        result.checked += 1
        if match.group(0) not in referable:
            record(Violation.DATE, match.group(0))

    supported = _figures(f"{cv}\n{context}")
    for figure, text in _iter_figures(generated):
        result.checked += 1
        if figure not in supported:
            record(Violation.METRIC, text)

    return result


@dataclass(frozen=True, slots=True)
class _Figure:
    value: float
    category: str


def _iter_figures(text: str) -> Iterator[tuple[_Figure, str]]:
    """Every quantified claim in `text`, as (value, category) with its wording."""
    for match in FIGURE_PATTERN.finditer(text):
        number = match.group("number")
        if not number:
            continue
        value = _value_of(number)
        if value is None:
            continue

        magnitude = match.group("magnitude")
        if magnitude:
            value *= 10 ** _MAGNITUDES[magnitude.casefold()]

        unit = (match.group("unit") or "").casefold()
        # A four-digit year is a date, whatever word follows it. DATE_PATTERN
        # owns those, and treating "2019 and" as a quantity blocked a sentence
        # that only stated when someone graduated.
        if not magnitude and 1900 <= value <= 2100 and value.is_integer() and len(number) == 4:
            continue
        yield _Figure(value=value, category=_category(unit)), match.group(0).strip()


def _figures(text: str) -> set[_Figure]:
    return {figure for figure, _ in _iter_figures(text)}


def _value_of(number: str) -> float | None:
    word = number.casefold()
    if word in _NUMBER_WORDS:
        return _NUMBER_WORDS[word]
    try:
        return float(number.replace(",", ""))
    except ValueError:
        return None


def _is_disclaimed(text: str, term: str) -> bool:
    """Is this mention explicitly disclaimed rather than claimed?"""
    lowered = normalize_for_match(text)
    index = lowered.find(normalize_for_match(term))
    if index < 0:
        return False
    start = max(0, index - NEGATION_WINDOW)
    end = min(len(text), index + len(term) + NEGATION_WINDOW)
    return NEGATION.search(text[start:end]) is not None


def _context(text: str, needle: str, window: int = 40) -> str:
    lowered = normalize_for_match(text)
    index = lowered.find(normalize_for_match(needle))
    if index < 0:
        return text[:window].strip()
    start = max(0, index - window)
    end = min(len(text), index + len(needle) + window)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


class InventionError(ValueError):
    """Generated text asserted something the source does not support."""

    def __init__(self, result: DiffResult) -> None:
        super().__init__(result.summary())
        self.result = result


def enforce(
    generated: str,
    *,
    cv: str,
    context: str = "",
    known_skills: frozenset[str] = frozenset(),
    known_employers: frozenset[str] = frozenset(),
) -> str:
    """Return `generated` if it is clean, or raise.

    The caller regenerates once and then gives up: a model that invents twice on
    the same input will invent a third time, and an honest failure is better
    than a fourth attempt that happens to slip through.
    """
    result = check(
        generated,
        cv=cv,
        context=context,
        known_skills=known_skills,
        known_employers=known_employers,
    )
    if not result.is_clean:
        raise InventionError(result)
    return generated
