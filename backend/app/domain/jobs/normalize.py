"""Cross-source normalisation helpers.

These are text utilities, not response accessors. Each adapter still owns its
own container shape and field vocabulary (Appendix B integration rule); what is
shared here is only what genuinely is shared: how a title is reduced to a
blocking key, how seniority is read out of a title, how HTML becomes text.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from datetime import UTC, datetime

from dateutil import parser as date_parser

from app.domain.models import EmploymentType, JobLocation, RemoteType, Seniority

# ── Script and language ───────────────────────────────────────────────

_ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_RANGE = re.compile(r"[A-Za-z]")
_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")


def detect_language(text: str) -> str:
    """Return 'ar' or 'en' by script share.

    Deliberately crude: the pipeline needs to know which analyser and which
    prompt language to use, not to identify Catalan. Mixed-script postings are
    common in MENA listings, so the threshold is a share, not a presence test.
    """
    if not text:
        return "en"
    sample = text[:4000]
    arabic = len(_ARABIC_RANGE.findall(sample))
    latin = len(_LATIN_RANGE.findall(sample))
    if arabic == 0:
        return "en"
    return "ar" if arabic / max(arabic + latin, 1) > 0.20 else "en"


def normalize_arabic(text: str) -> str:
    """Fold the orthographic variation that makes Arabic strings compare badly."""
    text = _TASHKEEL.sub("", text)
    text = text.replace("ـ", "")  # tatweel
    text = re.sub(r"[آأإٱ]", "ا", text)  # alef variants
    text = text.replace("ى", "ي")  # alef maqsura -> ya
    text = text.replace("ة", "ه")  # ta marbuta -> ha
    return text


# ── HTML ──────────────────────────────────────────────────────────────

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6]|section|article|ul|ol|table)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI = re.compile(r"<li\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+(\n)")


def html_to_text(html: str) -> str:
    """Flatten a job description to plain text, preserving list and block breaks.

    ATS descriptions are HTML fragments of wildly varying quality. A parser
    dependency buys little here and costs a wheel on ARM; the structure that
    matters for requirement extraction is the line break.
    """
    if not html:
        return ""
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _BR.sub("\n", text)
    text = _LI.sub("\n• ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _TRAILING_SPACE.sub(r"\1", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


# ── Titles ────────────────────────────────────────────────────────────

# Noise that appears inside titles across every ATS: gender tags, requisition
# ids, employment-type suffixes, location suffixes.
_TITLE_NOISE = (
    re.compile(r"\((?:[fmdwx]\s*/\s*)+[fmdwx]\)", re.I),  # (f/m/d), (m/w/d)
    re.compile(r"\b(?:[fmdwx]\s*/\s*){1,2}[fmdwx]\b", re.I),  # m/f/d without brackets
    re.compile(r"\b(?:job\s*)?(?:req(?:uisition)?|jr|id)[-#\s:]*\d{3,}\b", re.I),
    re.compile(r"#\s*\d{3,}"),
    re.compile(r"\b(?:full[-\s]?time|part[-\s]?time|permanent|contract|temporary)\b", re.I),
    re.compile(r"\b(?:remote|hybrid|on[-\s]?site|onsite|wfh)\b", re.I),
    re.compile(
        r"[\[\(][^\[\]\(\)]{0,40}(?:remote|hybrid|onsite|contract|intern(?:ship)?)[^\[\]\(\)]{0,40}[\]\)]",
        re.I,
    ),
)
_PUNCT = re.compile(r"[^\w\s\u0600-\u06FF+#]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Reduce a title to a stable, comparable form.

    Used for the blocking key in dedup (§11.3 stage 3) and for the exact-title
    index. Keeps `+` and `#` because `C++` and `C#` are different jobs.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    if _ARABIC_RANGE.search(text):
        text = normalize_arabic(text)
    text = text.casefold()
    for pattern in _TITLE_NOISE:
        text = pattern.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    # `_` is a word character, so it survives the punctuation pass — and it is a
    # single-character wildcard in the SQL LIKE that loads dedup candidates. No
    # job title needs one.
    text = text.replace("_", " ")
    return _WS.sub(" ", text).strip()


def title_tokens(title_normalized: str, n: int) -> tuple[str, ...]:
    """First `n` tokens of a normalised title — the blocking key's title half."""
    return tuple(title_normalized.split()[:n])


# ── Seniority ─────────────────────────────────────────────────────────

# Ordered most specific first: 'senior staff engineer' must not read as senior.
_SENIORITY_PATTERNS: tuple[tuple[re.Pattern[str], Seniority], ...] = (
    (
        re.compile(r"\b(?:intern(?:ship)?|trainee|placement|co[-\s]?op|working student)\b", re.I),
        Seniority.INTERN,
    ),
    (
        re.compile(r"\b(?:principal|distinguished|fellow|head of|vp|director)\b", re.I),
        Seniority.PRINCIPAL,
    ),
    (re.compile(r"\b(?:staff|lead|team lead|tech lead|architect)\b", re.I), Seniority.STAFF),
    (re.compile(r"\b(?:senior|sr\.?|snr|iii|3)\b", re.I), Seniority.SENIOR),
    (
        re.compile(
            r"\b(?:junior|jr\.?|entry[-\s]?level|graduate|grad|associate|i{1,2}\b|1)\b", re.I
        ),
        Seniority.JUNIOR,
    ),
    (re.compile(r"\b(?:mid[-\s]?level|intermediate|ii)\b", re.I), Seniority.MID),
)


def infer_seniority(title: str, description: str = "") -> Seniority | None:
    """Read a seniority level out of the title, falling back to the description.

    Returns None rather than guessing `mid`: an absent level is information the
    gate stage should see, and a fabricated one silently shifts every score.
    """
    for pattern, level in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    if description:
        head = description[:600]
        for pattern, level in _SENIORITY_PATTERNS:
            if pattern.search(head):
                return level
    return None


# ── Remote / employment type ──────────────────────────────────────────

_REMOTE = re.compile(r"\b(?:fully[-\s]?remote|remote[-\s]?first|100%\s*remote|remote)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(?:on[-\s]?site|onsite|in[-\s]?office)\b", re.I)


def infer_remote_type(*fields: str | None) -> RemoteType | None:
    """Hybrid wins over remote when both appear: 'remote-friendly hybrid' is hybrid."""
    blob = " ".join(f for f in fields if f)
    if not blob:
        return None
    if _HYBRID.search(blob):
        return RemoteType.HYBRID
    if _REMOTE.search(blob):
        return RemoteType.REMOTE
    if _ONSITE.search(blob):
        return RemoteType.ONSITE
    return None


_EMPLOYMENT_MAP: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (re.compile(r"intern", re.I), EmploymentType.INTERNSHIP),
    (re.compile(r"part[-\s_]?time", re.I), EmploymentType.PART_TIME),
    (re.compile(r"contract|freelance|b2b", re.I), EmploymentType.CONTRACT),
    (re.compile(r"tempor", re.I), EmploymentType.TEMPORARY),
    (re.compile(r"full[-\s_]?time|permanent|regular", re.I), EmploymentType.FULL_TIME),
)


def parse_employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    for pattern, kind in _EMPLOYMENT_MAP:
        if pattern.search(value):
            return kind
    return EmploymentType.OTHER


# ── Locations ─────────────────────────────────────────────────────────

_COUNTRY_HINTS: dict[str, str] = {
    "egypt": "EG",
    "مصر": "EG",
    "cairo": "EG",
    "giza": "EG",
    "alexandria": "EG",
    "united arab emirates": "AE",
    "uae": "AE",
    "dubai": "AE",
    "abu dhabi": "AE",
    "saudi arabia": "SA",
    "riyadh": "SA",
    "jeddah": "SA",
    "united kingdom": "GB",
    "uk": "GB",
    "london": "GB",
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "new york": "US",
    "san francisco": "US",
    "germany": "DE",
    "berlin": "DE",
    "munich": "DE",
    "netherlands": "NL",
    "amsterdam": "NL",
    "france": "FR",
    "paris": "FR",
    "spain": "ES",
    "madrid": "ES",
    "barcelona": "ES",
    "poland": "PL",
    "warsaw": "PL",
    "krakow": "PL",
    "ireland": "IE",
    "dublin": "IE",
    "canada": "CA",
    "toronto": "CA",
    "india": "IN",
    "bangalore": "IN",
    "bengaluru": "IN",
    "jordan": "JO",
    "amman": "JO",
    "morocco": "MA",
    "casablanca": "MA",
    "tunisia": "TN",
    "qatar": "QA",
    "doha": "QA",
    "kuwait": "KW",
    "bahrain": "BH",
    "turkey": "TR",
    "istanbul": "TR",
    "portugal": "PT",
    "lisbon": "PT",
    "romania": "RO",
    "bucharest": "RO",
    "czech": "CZ",
    "prague": "CZ",
}


def parse_location(raw: str | None) -> JobLocation | None:
    """Split a free-text location string into city / region / country.

    ATS location strings are unstructured ('Cairo, Egypt (Remote)'), so this is
    a heuristic that fills what it can and keeps `raw` for everything else. No
    field is invented: an unrecognised country stays None.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    lowered = text.casefold()
    is_remote = bool(_REMOTE.search(lowered))

    country: str | None = None
    for hint, code in _COUNTRY_HINTS.items():
        if re.search(rf"\b{re.escape(hint)}\b", lowered):
            country = code
            break

    parts = [p.strip() for p in re.split(r"[,;|]| - ", text) if p.strip()]
    city = parts[0] if parts else None
    if city and _REMOTE.fullmatch(city.strip()):
        city = None
    region = parts[1] if len(parts) > 2 else None
    return JobLocation(raw=text, city=city, region=region, country=country, is_remote=is_remote)


# ── Dates ─────────────────────────────────────────────────────────────


def parse_datetime(value: object) -> datetime | None:
    """Parse the several date shapes ATS feeds emit, always returning UTC-aware.

    Handles ISO strings, epoch seconds and epoch milliseconds — Lever emits
    milliseconds, Greenhouse emits ISO, and mixing them up silently makes every
    posting look 55 years old, which the freshness gate would then discard.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_datetime(int(text))
        try:
            parsed = date_parser.isoparse(text)
        except ValueError:
            try:
                parsed = date_parser.parse(text)
            except (ValueError, OverflowError):
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
