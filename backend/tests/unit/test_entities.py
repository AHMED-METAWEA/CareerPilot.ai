"""Company entity resolution (§11.3, R5).

The asymmetry under test: a false merge corrupts two employers' data and is
hard to detect; an unresolved employer costs one row in a review queue.
"""

from __future__ import annotations

import pytest

from app.domain.dedup.entities import KnownCompany, normalize_company_name, resolve_company
from app.domain.models import CompanyRef

KNOWN = [
    KnownCompany(
        key="vodafone",
        canonical_name="Vodafone",
        domain="vodafone.com",
        aliases=("_VOIS", "Vodafone Egypt", "فودافون مصر"),
    ),
    KnownCompany(key="instabug", canonical_name="Instabug", domain="instabug.com"),
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Instabug, Inc.", "instabug"),
        ("Vodafone Egypt S.A.E.", "vodafone egypt"),
        ("Acme GmbH", "acme"),
        ("Acme B.V.", "acme"),
        ("Smith & Sons Ltd", "smith and sons"),
        ("شركة فودافون مصر", "فودافون مصر"),
    ],
)
def test_normalize_company_name(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_descriptor_words_are_not_stripped() -> None:
    """'Solutions' and 'Group' are part of a name, not a legal form. Stripping
    them collapses 'Vodafone Intelligent Solutions' into 'Vodafone' — the exact
    false merge R5 warns about."""
    assert (
        normalize_company_name("Vodafone Intelligent Solutions") == "vodafone intelligent solutions"
    )
    assert normalize_company_name("Group Nine Media") == "group nine media"


def test_domain_is_the_strongest_signal() -> None:
    resolution = resolve_company(
        CompanyRef(name="Totally Different Name", domain="instabug.com"), KNOWN
    )
    assert (resolution.company_key, resolution.method) == ("instabug", "domain")


def test_curated_alias_resolves_exactly() -> None:
    for name in ("_VOIS", "Vodafone Egypt", "فودافون مصر"):
        resolution = resolve_company(CompanyRef(name=name), KNOWN)
        assert resolution.company_key == "vodafone", name
        assert resolution.method == "alias"


def test_legal_suffix_does_not_defeat_the_alias_table() -> None:
    assert resolve_company(CompanyRef(name="Instabug LLC"), KNOWN).company_key == "instabug"


def test_typo_resolves_by_fuzzy_match() -> None:
    resolution = resolve_company(CompanyRef(name="Instabugg"), KNOWN)
    assert (resolution.company_key, resolution.method) == ("instabug", "fuzzy")
    assert resolution.confidence >= 0.90


@pytest.mark.parametrize("name", ["Vodafone Germany", "Vodafone Intelligent Solutions"])
def test_subsidiary_shaped_names_go_to_review_not_to_a_merge(name: str) -> None:
    """A strict token subset is the shape of a regional subsidiary. It is never
    auto-merged and never silently filed as an unrelated new employer."""
    resolution = resolve_company(CompanyRef(name=name), KNOWN)
    assert resolution.company_key is None
    assert resolution.needs_review is True
    assert resolution.matched_alias is not None


def test_unrelated_company_is_new_without_review() -> None:
    resolution = resolve_company(CompanyRef(name="Swvl"), KNOWN)
    assert (resolution.company_key, resolution.needs_review) == (None, False)


def test_empty_name_is_always_reviewed() -> None:
    resolution = resolve_company(CompanyRef(name="   "), KNOWN)
    assert resolution.needs_review is True


def test_thresholds_are_configurable() -> None:
    strict = resolve_company(CompanyRef(name="Instabugg"), KNOWN, fuzzy_threshold=0.99)
    assert strict.company_key is None and strict.needs_review is True


# ── ATS hosts are not employer domains ────────────────────────────────
#
# Regression: deriving a company domain from a careers or apply URL on a shared
# ATS host merged thirteen unrelated employers into one row during the first
# real corpus build. The row looked entirely plausible from the outside.


def test_ats_host_is_never_an_employer_domain() -> None:
    from app.domain.jobs.urls import company_domain, is_ats_host

    assert is_ats_host("https://boards.greenhouse.io/adyen/jobs/1") is True
    assert is_ats_host("https://acme.recruitee.com/o/engineer") is True
    assert is_ats_host("https://careers.adyen.com") is False
    assert company_domain(None, "https://job-boards.greenhouse.io/adyen/jobs/1") is None
    assert company_domain(None, "https://careers.adyen.com/jobs/1") == "adyen.com"
    # A real domain wins over an ATS one regardless of order.
    assert company_domain("https://jobs.lever.co/x", "https://acme.com") == "acme.com"


def test_two_employers_on_the_same_ats_do_not_merge() -> None:
    known = [
        KnownCompany(
            key="adyen",
            canonical_name="Adyen",
            domain=None,
            aliases=(),
        )
    ]
    resolution = resolve_company(
        CompanyRef(name="Algolia", careers_url="https://job-boards.greenhouse.io/algolia/jobs/1"),
        known,
    )
    assert resolution.company_key is None
    assert resolution.method == "new"


def test_a_stored_ats_domain_cannot_capture_new_employers() -> None:
    """Defence in depth: even if a bad domain reached the table, it must not match."""
    known = [KnownCompany(key="adyen", canonical_name="Adyen", domain="greenhouse.io")]
    resolution = resolve_company(
        CompanyRef(name="Wolt", careers_url="https://boards.greenhouse.io/wolt/jobs/2"), known
    )
    assert resolution.company_key is None
