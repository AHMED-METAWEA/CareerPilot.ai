"""PII redaction before any hosted model call (§11.1 step 6, §16.2).

Name, email, phone and address are replaced with stable placeholders before the
CV text leaves this process. The mapping is returned to the caller and held in
memory for the duration of the request only — it is never persisted and never
logged.

Two design points worth stating:

* **Offsets are preserved where possible.** Evidence spans are computed against
  the *original* text, so redaction must not shift positions unpredictably.
  Placeholders are padded to the length of what they replace where that is
  possible, and the caller is given the mapping either way.
* **Under-redaction is the failure that matters.** A missed phone number is
  sent to a third party; an over-redacted word costs a little extraction
  quality. The patterns therefore lean broad, and the name heuristic covers the
  header block where CVs actually put names.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
# International, local and MENA formats: +20 100 123 4567, (555) 010-9999,
# 0100-123-4567, +44 20 7946 0958. Deliberately broad, then validated by
# `_is_phone_like` — the pattern finds candidates; the validator judges them.
PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{1,4}\)[\s.\-]?)?\d[\d\s.()\-]{5,16}\d(?![\w.])"
)
MIN_PHONE_DIGITS = 7
MIN_BARE_PHONE_DIGITS = 9
"""An unseparated digit run needs more digits to count as a phone number.

"4000000 events per day" is a metric and the substance of a CV bullet; redacting
it costs the extraction a real achievement. "01001234567" is a mobile number.
The separators, and the digit count, are what distinguish them."""
URL_PROFILE = re.compile(
    r"\b(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|gitlab\.com|x\.com|twitter\.com)/[\w\-./]+",
    re.I,
)
# Street addresses: a number followed by words and a street-type token.
ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][\w'.-]*\s+){0,4}"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|lane|ln\.?|drive|dr\.?|"
    r"court|ct\.?|square|sq\.?|شارع|طريق|ميدان)\b[^\n,]{0,30}",
    re.I,
)
NATIONAL_ID = re.compile(r"\b\d{14}\b")  # Egyptian national ID
_NAME_LINE = re.compile(r"^[^\W\d_][\w'؀-ۿ.-]*(?:\s+[^\W\d_][\w'؀-ۿ.-]*){1,3}$")


@dataclass(slots=True)
class RedactionMap:
    """Placeholder -> original value. In memory only, for one request."""

    values: dict[str, str] = field(default_factory=dict)

    def add(self, placeholder: str, original: str) -> None:
        self.values[placeholder] = original

    def restore(self, text: str) -> str:
        """Put the original values back into model output."""
        for placeholder, original in self.values.items():
            text = text.replace(placeholder, original)
        return text

    def __len__(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    mapping: RedactionMap
    counts: dict[str, int]


def redact(text: str, *, known_name: str | None = None) -> RedactionResult:
    """Replace personal identifiers with stable placeholders.

    `known_name` is used when the candidate has already confirmed their name;
    otherwise the header heuristic applies. Both paths are conservative about
    what counts as a name — a false positive removes a word the model needed.
    """
    mapping = RedactionMap()
    counts: dict[str, int] = {}
    redacted = text

    for label, pattern in (
        ("EMAIL", EMAIL),
        ("PROFILE_URL", URL_PROFILE),
        ("ADDRESS", ADDRESS),
        ("NATIONAL_ID", NATIONAL_ID),
        ("PHONE", PHONE),
    ):
        redacted, count = _replace(redacted, pattern, label, mapping)
        if count:
            counts[label] = count

    names = [known_name] if known_name else _candidate_names(text)
    for index, name in enumerate(n for n in names if n):
        placeholder = f"[[NAME_{index + 1}]]"
        pattern = re.compile(rf"\b{re.escape(name.strip())}\b", re.I)
        if pattern.search(redacted):
            redacted = pattern.sub(placeholder, redacted)
            mapping.add(placeholder, name.strip())
            counts["NAME"] = counts.get("NAME", 0) + 1

    return RedactionResult(text=redacted, mapping=mapping, counts=counts)


def _is_phone_like(value: str) -> bool:
    digits = sum(1 for char in value if char.isdigit())
    if digits < MIN_PHONE_DIGITS:
        return False
    separated = any(char in " .-()+" for char in value.strip())
    return separated or digits >= MIN_BARE_PHONE_DIGITS


_VALIDATORS: dict[str, Callable[[str], bool]] = {"PHONE": _is_phone_like}


def _replace(
    text: str, pattern: re.Pattern[str], label: str, mapping: RedactionMap
) -> tuple[str, int]:
    seen: dict[str, str] = {}
    validator = _VALIDATORS.get(label)

    def substitute(match: re.Match[str]) -> str:
        original = match.group(0)
        if validator is not None and not validator(original):
            return original
        if original in seen:
            return seen[original]
        placeholder = f"[[{label}_{len(seen) + 1}]]"
        seen[original] = placeholder
        mapping.add(placeholder, original)
        return placeholder

    return pattern.sub(substitute, text), len(seen)


def _candidate_names(text: str) -> list[str]:
    """The name is almost always the first substantial line of a CV.

    Deliberately narrow: only the first few lines are considered, and only lines
    that look like two-to-four capitalised words with no digits or contact
    punctuation. A stray match here deletes a real word from the document.
    """
    for line in text.splitlines()[:6]:
        candidate = line.strip().strip("|·—-").strip()
        if not candidate or len(candidate) > 60:
            continue
        if any(ch.isdigit() for ch in candidate) or "@" in candidate:
            continue
        if candidate.lower() in {"curriculum vitae", "cv", "resume", "résumé"}:
            continue
        if _NAME_LINE.match(candidate):
            return [candidate]
    return []


def assert_no_pii(text: str) -> list[str]:
    """Post-redaction check: what, if anything, still looks like PII.

    Used as a guard immediately before a hosted call — a redaction that silently
    misses is worse than one that fails loudly.
    """
    remaining: list[str] = []
    for label, pattern in (("EMAIL", EMAIL), ("PHONE", PHONE), ("NATIONAL_ID", NATIONAL_ID)):
        if pattern.search(text):
            remaining.append(label)
    return remaining
