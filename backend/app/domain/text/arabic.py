"""Arabic and mixed-script text handling (§18, Phase 5).

MENA CVs and job descriptions are rarely in one script. A posting reads
"مطلوب مهندس بيانات لديه خبرة في Python وKafka", a CV lists sections in Arabic
and technologies in Latin, and both arrive with whatever the PDF extractor left
behind. Four things in that sentence break naive processing, and each has a
function here:

* **Bidi control characters.** Extractors emit U+200F, U+202B and friends to
  preserve visual order. They are invisible, they are `\\w`-adjacent, and they
  silently break every regex boundary and every string comparison.
* **Arabic-Indic digits.** "٥ سنوات" is five years, and `int("٥")` happens to
  work while `re.match(r"\\d", "٥")` also matches — but `float("٥,٠٠٠")` does
  not, and a figure written "٤ مليون" never matches a CV's "4 million".
* **Orthographic variation.** أ إ آ ٱ are all ا in practice; ة and ه, ى and ي
  are written interchangeably. Comparing unfolded Arabic strings finds nothing.
* **Clitics.** Arabic attaches conjunctions and prepositions directly to the
  following word, including Latin ones: "وPython" is "and Python". No space
  separates them, so a word-boundary match for `Python` fails — an Arabic letter
  is a word character to `\\w`, so the lookaround that keeps `Go` out of
  `Google` also keeps `Python` out of `وPython`.

**Offsets.** `segment_scripts` inserts characters, so it changes offsets. It is
for matching only. Anything that records a span into stored text (§10.1 span
verification, `profile_skills.evidence_span`) must run against the original, or
the quote shown to a candidate will not be the quote that was verified.
"""

from __future__ import annotations

import re
import unicodedata

# ── Character classes ─────────────────────────────────────────────────

ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RANGE = re.compile(r"[A-Za-z]")

_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = "ـ"

# Left-to-right and right-to-left marks, embeddings, overrides and isolates.
# PDF and DOCX extractors emit these to preserve visual order; they carry no
# meaning in extracted text and corrupt every boundary assertion downstream.
_BIDI_CONTROLS = re.compile(r"[\u200E\u200F\u202A-\u202E\u2066-\u2069\u061C]")

# Zero-width characters, which arrive from the same place and are just as
# invisible. ZWNJ is meaningful in Persian; in Arabic CVs it is extractor noise.
_ZERO_WIDTH = re.compile(r"[\u200B-\u200D\uFEFF]")

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹, used for Persian/Urdu).
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")} | {
    ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")
}

# Arabic punctuation, folded so that a comma is a comma whatever the keyboard.
_PUNCTUATION_MAP = {
    ord("،"): ",",
    ord("؛"): ";",
    ord("؟"): "?",
    ord("٪"): "%",
    ord("۔"): ".",
    ord("٫"): ".",  # Arabic decimal separator
    ord("٬"): ",",  # Arabic thousands separator
}


def strip_invisibles(text: str) -> str:
    """Remove bidi controls and zero-width characters.

    First, always: every other function here asserts boundaries, and an
    invisible character sitting between a digit and its unit defeats all of them.
    """
    return _ZERO_WIDTH.sub("", _BIDI_CONTROLS.sub("", text))


def fold_digits(text: str) -> str:
    """Arabic-Indic and Extended Arabic-Indic digits to ASCII.

    Done everywhere rather than at the point of parsing: a year, a phone number
    and a metric are all numbers, and code that reads one of them should not
    have to know which keyboard typed it.
    """
    return text.translate(_DIGIT_MAP)


def normalize_arabic(text: str) -> str:
    """Fold the variation that makes Arabic strings compare badly.

    Lossy on purpose. ة → ه and ى → ي lose a distinction that matters in
    careful writing and does not survive the way people actually type CVs; a
    comparison that respects it finds nothing.
    """
    text = strip_invisibles(text)
    text = _TASHKEEL.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = re.sub(r"[آأإٱ]", "ا", text)
    text = text.replace("ى", "ي")
    text = text.replace("ة", "ه")
    text = fold_digits(text)
    return text.translate(_PUNCTUATION_MAP)


# ── Mixed script ──────────────────────────────────────────────────────

# An Arabic letter immediately followed by a Latin one, or the reverse. Both
# directions occur: "وPython" (clitic before) and "Pythonو" (rare, but produced
# by extractors that reorder runs).
_AR_THEN_LATIN = re.compile(r"([\u0600-\u06FF])([A-Za-z])")
_LATIN_THEN_AR = re.compile(r"([A-Za-z])([\u0600-\u06FF])")


def segment_scripts(text: str) -> str:
    """Insert a space at every Arabic↔Latin boundary.

    "خبرة في Python وKafka" becomes "... و Kafka", so `Kafka` is findable by a
    word-boundary match. Without this the taxonomy misses every skill a clitic
    is attached to, and the anti-invention diff (§10.3) reads such a skill as
    absent from the CV — which turns a true statement into a refusal.

    Changes offsets. Matching only; never for spans.
    """
    text = _AR_THEN_LATIN.sub(r"\1 \2", text)
    return _LATIN_THEN_AR.sub(r"\1 \2", text)


def normalize_for_matching(text: str) -> str:
    """The full fold used when comparing text to text.

    NFKC first, so Arabic presentation forms (ﻻ, ﻢ — one code point per glyph
    shape, emitted by older PDF producers) become ordinary letters before
    anything tries to fold them.
    """
    return segment_scripts(normalize_arabic(unicodedata.normalize("NFKC", text))).casefold()


def script_shares(text: str) -> tuple[float, float]:
    """(Arabic share, Latin share) of the letters in `text`."""
    sample = text[:4000]
    arabic = len(ARABIC_RANGE.findall(sample))
    latin = len(LATIN_RANGE.findall(sample))
    total = arabic + latin
    if total == 0:
        return 0.0, 0.0
    return arabic / total, latin / total


MIXED_SCRIPT_FLOOR = 0.10
"""Below this share, a script is incidental rather than present. A job ad in
English naming one Arabic company is not a mixed-script posting; one whose
requirements are Arabic and whose technologies are Latin is."""


def detect_language(text: str) -> str:
    """Return 'ar' or 'en' by script share.

    Deliberately crude: the pipeline needs to know which analyser and which
    prompt language to use, not to identify Catalan. Mixed-script postings are
    the norm in MENA listings, so this is a share and not a presence test.
    """
    if not text:
        return "en"
    arabic, _ = script_shares(text)
    return "ar" if arabic > 0.20 else "en"


def is_mixed_script(text: str) -> bool:
    """Does this text genuinely use both scripts?

    Reported rather than resolved: a mixed-script CV is normal, and the pipeline
    needs to know so it can run both analysers rather than pick one and lose
    half the document.
    """
    arabic, latin = script_shares(text)
    return arabic >= MIXED_SCRIPT_FLOOR and latin >= MIXED_SCRIPT_FLOOR


# ── Vocabulary ────────────────────────────────────────────────────────

# Arabic section headings, for CV structure detection. Written as alternations
# of the *folded* forms, so they are matched against `normalize_arabic` output
# rather than against whatever the candidate typed.
SECTION_VOCABULARY: dict[str, tuple[str, ...]] = {
    "experience": ("الخبره", "الخبرات", "الخبره العمليه", "السيره المهنيه", "التاريخ الوظيفي"),
    "education": ("التعليم", "المؤهلات", "المؤهل العلمي", "الدراسه", "التعليم الاكاديمي"),
    "skills": ("المهارات", "المهارات التقنيه", "المهارات الشخصيه", "القدرات", "الكفاءات"),
    "languages": ("اللغات", "اللغه"),
    "certifications": ("الشهادات", "الدورات", "الدورات التدريبيه", "التدريب"),
    "projects": ("المشاريع", "المشروعات", "الاعمال"),
    "summary": ("نبذه", "الملخص", "نبذه شخصيه", "الهدف الوظيفي", "عن نفسي"),
}

# Seniority and employment vocabulary, for the fields §7.1 reads from a title.
# The labels are the `Seniority`, `RemoteType` and `EmploymentType` values
# themselves: an Arabic title has to produce a value from the same closed set as
# an English one, or the gate stage is comparing two different vocabularies.
SENIORITY_VOCABULARY: dict[str, tuple[str, ...]] = {
    "intern": ("متدرب", "متدربه", "تدريب"),
    "junior": ("مبتدئ", "مبتدىء", "حديث التخرج", "خريج جديد", "مساعد"),
    "mid": ("متوسط", "خبره متوسطه"),
    "senior": ("اول", "كبير", "خبير", "سينيور"),
    "staff": ("قائد فريق", "رئيس فريق", "مشرف", "معماري"),
    "principal": ("مدير تنفيذي", "مدير عام", "رئيس قطاع", "مدير اداره"),
}
"""Note what is absent: "مدير" alone. A manager is a role, not a rung on this
ladder, and the English path does not map it either — inventing a level for it
would shift every score for every posting that says it."""

REMOTE_VOCABULARY: dict[str, tuple[str, ...]] = {
    "remote": ("عن بعد", "من المنزل", "ريموت", "عمل عن بعد"),
    "hybrid": ("هجين", "مختلط", "نظام هجين"),
    "onsite": ("من المكتب", "حضوري", "في الموقع", "بالمقر"),
}

EMPLOYMENT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "internship": ("تدريب صيفي", "برنامج تدريبي", "تدريب"),
    "part_time": ("دوام جزئي", "بدوام جزئي", "جزئي"),
    "temporary": ("مؤقت", "موسمي"),
    "contract": ("عقد", "بعقد", "تعاقد", "حر"),
    "full_time": ("دوام كامل", "بدوام كامل", "وظيفه كامله", "تعيين دائم"),
}

# "خمس سنوات خبرة" — years of experience written as words. Only the range a CV
# actually uses; beyond ten, people write digits.
_NUMBER_WORDS: dict[str, int] = {
    "سنه": 1,
    "سنتين": 2,
    "سنتان": 2,
    "ثلاث": 3,
    "اربع": 4,
    "خمس": 5,
    "ست": 6,
    "سبع": 7,
    "ثمان": 8,
    "ثماني": 8,
    "تسع": 9,
    "عشر": 10,
    "عشره": 10,
    "خمسه عشر": 15,
    "عشرين": 20,
}

_YEARS_UNIT = r"(?:سنه|سنوات|سنين|عام|اعوام)"
_YEARS_DIGITS = re.compile(rf"(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS_UNIT}")
_YEARS_WORDS = re.compile(
    rf"\b({'|'.join(sorted(_NUMBER_WORDS, key=len, reverse=True))})\s+{_YEARS_UNIT}"
)
# Arabic marks "two" with a dual ending rather than a separate word, so "سنتين"
# is "two years" with no unit to match. A rule that requires number-then-unit
# misses the single most common way a junior candidate states their experience.
_YEARS_DUAL = re.compile(r"\b(?:سنتين|سنتان|عامين|عامان)\b")


def extract_years_of_experience(text: str) -> float | None:
    """Years stated in Arabic, in digits or in words.

    Returns the largest figure found, because a CV that says "خمس سنوات" in one
    place and "٧ سنوات خبرة" in another is describing a career, not two jobs.
    Returns None rather than a guess when nothing is stated — §7.1's rule that a
    null beats an invented number applies in both languages.
    """
    folded = normalize_arabic(unicodedata.normalize("NFKC", text))
    values: list[float] = [float(match.group(1)) for match in _YEARS_DIGITS.finditer(folded)]
    values += [float(_NUMBER_WORDS[match.group(1)]) for match in _YEARS_WORDS.finditer(folded)]
    if _YEARS_DUAL.search(folded):
        values.append(2.0)
    # A CV is not 60 years long; a number that large is a date or a quantity
    # that happened to sit next to the word "years".
    plausible = [value for value in values if 0 < value <= 60]
    return max(plausible) if plausible else None


def _match_vocabulary(text: str, vocabulary: dict[str, tuple[str, ...]]) -> str | None:
    """First label whose phrases appear, longest phrase first.

    Longest-first matters: "مدير تنفيذي" (director) contains "مدير" (manager),
    and checking in dictionary order would call every director a manager.
    """
    folded = normalize_arabic(unicodedata.normalize("NFKC", text))
    candidates = sorted(
        ((phrase, label) for label, phrases in vocabulary.items() for phrase in phrases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for phrase, label in candidates:
        if normalize_arabic(phrase) in folded:
            return label
    return None


def detect_seniority(title: str) -> str | None:
    """Seniority from an Arabic title, or None where it is not stated."""
    return _match_vocabulary(title, SENIORITY_VOCABULARY)


def detect_remote_type(text: str) -> str | None:
    return _match_vocabulary(text, REMOTE_VOCABULARY)


def detect_employment_type(text: str) -> str | None:
    return _match_vocabulary(text, EMPLOYMENT_VOCABULARY)


def detect_sections(text: str) -> frozenset[str]:
    """Which CV sections this Arabic text has headings for."""
    folded = normalize_arabic(unicodedata.normalize("NFKC", text))
    return frozenset(
        section
        for section, headings in SECTION_VOCABULARY.items()
        if any(normalize_arabic(heading) in folded for heading in headings)
    )
