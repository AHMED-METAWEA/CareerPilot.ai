"""Company entity resolution (§11.3).

The genuine difficulty in deduplication is the employer, not the text.
"Vodafone Egypt", "_VOIS", "Vodafone Intelligent Solutions" and "فودافون مصر"
must resolve to one entity; "Vodafone Egypt" and "Vodafone Germany" must not.

Precedence: registrable domain (near-certain) > curated alias (certain, because
a human wrote it) > fuzzy name match (evidence, not proof) > new company.

The asymmetry that matters is R5: a false merge corrupts two employers' data
and is hard to detect, while an unresolved company costs one row in a review
queue. So the fuzzy stage is deliberately conservative, and a name that is a
strict token subset of another ("Vodafone" ⊂ "Vodafone Egypt") is *never*
auto-merged — that is the exact shape a regional subsidiary takes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.domain.jobs.normalize import normalize_arabic
from app.domain.jobs.urls import company_domain
from app.domain.models import CompanyRef, CompanyResolution

_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "plc",
    "corp",
    "corporation",
    "co",
    "company",
    "gmbh",
    "mbh",
    "ag",
    "kg",
    "ohg",
    "ug",
    "se",
    "bv",
    "nv",
    "sa",
    "sas",
    "sarl",
    "srl",
    "spa",
    "sae",
    "ab",
    "as",
    "oy",
    "oyj",
    "aps",
    "pty",
    "llp",
    "lp",
}
"""Legal forms only. Descriptors such as 'Group', 'Solutions' or 'Technologies'
are deliberately kept: dropping them collapses 'Vodafone Intelligent Solutions'
into 'Vodafone', which is exactly the false merge R5 warns about. Those cases
belong in the curated alias table, asserted by a human."""

_ARABIC_LEGAL = {"شركه", "شركة", "مجموعه", "مجموعة", "القابضه", "القابضة"}

_PUNCT = re.compile(r"[^\w\s\u0600-\u06FF&+]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """Casefold, strip legal-form noise, fold Arabic orthography.

    Legal suffixes are dropped only when they are trailing tokens: 'Group' at
    the end of 'Vodafone Group' is noise, but 'Group' inside 'Group Nine Media'
    is the name.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name)
    text = normalize_arabic(text)
    text = text.casefold()
    text = text.replace("&", " and ")
    text = text.replace(".", "")  # S.A.E. -> SAE, B.V. -> BV, before splitting
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.split(text) if t]
    while tokens and (tokens[-1] in _LEGAL_SUFFIXES or tokens[-1] in _ARABIC_LEGAL):
        tokens.pop()
    while tokens and tokens[0] in _ARABIC_LEGAL:
        tokens.pop(0)
    return " ".join(tokens)


@dataclass(frozen=True, slots=True)
class KnownCompany:
    """An employer already in the database, projected for matching."""

    key: str
    canonical_name: str
    domain: str | None = None
    aliases: tuple[str, ...] = field(default=())


def _name_similarity(a: str, b: str) -> float:
    """0–1 similarity that penalises word-order-independent subset matches.

    `token_set_ratio` alone reports 1.00 for "vodafone" vs "vodafone egypt";
    taking the minimum with `token_sort_ratio` keeps genuine reorderings high
    while pulling subsets down into the review band.
    """
    if not a or not b:
        return 0.0
    return min(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0


def _containment(a: str, b: str) -> float:
    """Share of the shorter name's tokens present in the longer one."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _is_strict_subset(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    return bool(ta) and bool(tb) and (ta < tb or tb < ta)


def resolve_company(
    ref: CompanyRef,
    known: Sequence[KnownCompany],
    *,
    fuzzy_threshold: float = 0.90,
    review_threshold: float = 0.75,
) -> CompanyResolution:
    """Resolve an incoming employer reference against the known set."""
    # A careers URL on a shared ATS host identifies the platform, not the
    # employer, and is discarded here rather than becoming a company key.
    incoming_domain = company_domain(ref.domain, ref.careers_url)

    # 1 ── Registrable domain: the strongest signal available.
    if incoming_domain:
        for company in known:
            if company.domain and company_domain(company.domain) == incoming_domain:
                return CompanyResolution(company_key=company.key, confidence=0.99, method="domain")

    normalized = normalize_company_name(ref.name)
    if not normalized:
        return CompanyResolution(company_key=None, confidence=0.0, method="new", needs_review=True)

    # 2 ── Curated alias: a human already asserted this equivalence.
    for company in known:
        for alias in (company.canonical_name, *company.aliases):
            if normalize_company_name(alias) == normalized:
                return CompanyResolution(
                    company_key=company.key,
                    confidence=1.0,
                    method="alias",
                    matched_alias=alias,
                )

    # 3 ── Fuzzy: evidence, and only above the threshold, and never on a subset.
    best: tuple[float, KnownCompany, str] | None = None
    subset_hit: tuple[float, KnownCompany, str] | None = None
    for company in known:
        for alias in (company.canonical_name, *company.aliases):
            alias_normalized = normalize_company_name(alias)
            score = _name_similarity(normalized, alias_normalized)
            if best is None or score > best[0]:
                best = (score, company, alias)
            # Tracked independently of `best`: a subsidiary-shaped name can be
            # a strict subset of one alias while scoring higher against another.
            if _is_strict_subset(normalized, alias_normalized) and (
                fuzz.token_set_ratio(normalized, alias_normalized) >= 95
            ):
                containment = _containment(normalized, alias_normalized)
                if subset_hit is None or containment > subset_hit[0]:
                    subset_hit = (containment, company, alias)

    if best is not None and subset_hit is None and best[0] >= fuzzy_threshold:
        score, company, alias = best
        return CompanyResolution(
            company_key=company.key, confidence=score, method="fuzzy", matched_alias=alias
        )

    # A strict token subset is the shape of a regional subsidiary or a business
    # unit ("Vodafone" ⊂ "Vodafone Egypt"). It always goes to a human: never an
    # auto-merge, and never a silent new company row either.
    if subset_hit is not None:
        _, company, alias = subset_hit
        return CompanyResolution(
            company_key=None,
            # Reported confidence is name similarity, not containment: it is
            # what a reviewer needs to see, and it stays comparable across rows.
            confidence=_name_similarity(normalized, normalize_company_name(alias)),
            method="new",
            needs_review=True,
            matched_alias=alias,
        )

    if best is not None and best[0] >= review_threshold:
        return CompanyResolution(
            company_key=None,
            confidence=best[0],
            method="new",
            needs_review=True,
            matched_alias=best[2],
        )

    # 4 ── Genuinely new employer. No review needed: nothing was ambiguous.
    return CompanyResolution(company_key=None, confidence=0.0, method="new", needs_review=False)
