"""URL canonicalisation and apply-URL precedence (§11.2 step 7, Appendix D)."""

from __future__ import annotations

import pytest

from app.domain.jobs.urls import (
    canonicalize_url,
    registrable_domain,
    resolve_apply_url,
    strip_tracking_params,
    url_host,
)
from app.domain.models import ApplyUrlMethod


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/1?gh_src=abc",
            "https://boards.greenhouse.io/acme/jobs/1",
        ),
        ("HTTPS://WWW.Example.COM:443/Jobs/1/", "https://example.com/Jobs/1"),
        ("http://example.com:80/x?utm_source=n&b=2&a=1", "http://example.com/x?a=1&b=2"),
        ("https://example.com/x#apply", "https://example.com/x"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_canonicalisation_is_idempotent() -> None:
    once = canonicalize_url("https://WWW.Example.com/a/?utm_campaign=x&z=1&a=2")
    assert canonicalize_url(once) == once


def test_strip_tracking_preserves_visitable_url() -> None:
    """The stored apply URL keeps its case, path and non-tracking query."""
    url = "https://jobs.lever.co/Acme/ID-42?utm_source=x&lever-origin=applied&team=Data"
    stripped = strip_tracking_params(url)
    assert "utm_source" not in stripped
    assert "lever-origin=applied" in stripped
    assert "/Acme/ID-42" in stripped


def test_apply_url_precedence_prefers_ats_native() -> None:
    resolved = resolve_apply_url(
        ats_native="https://boards.greenhouse.io/acme/jobs/1",
        canonical_link="https://acme.com/careers/1",
        json_ld_url="https://acme.com/jsonld/1",
        redirect_terminus="https://acme.com/final",
        source_url="https://aggregator.example/1",
    )
    assert resolved.method is ApplyUrlMethod.ATS_NATIVE
    assert resolved.url == "https://boards.greenhouse.io/acme/jobs/1"


@pytest.mark.parametrize(
    ("kwargs", "method"),
    [
        ({"canonical_link": "https://a.co/1"}, ApplyUrlMethod.CANONICAL_LINK),
        ({"json_ld_url": "https://a.co/1"}, ApplyUrlMethod.JSON_LD),
        ({"redirect_terminus": "https://a.co/1"}, ApplyUrlMethod.REDIRECT_TERMINUS),
        ({}, ApplyUrlMethod.SOURCE_URL),
    ],
)
def test_apply_url_falls_through_in_order(kwargs: dict[str, str], method: ApplyUrlMethod) -> None:
    resolved = resolve_apply_url(source_url="https://source.example/1", **kwargs)
    assert resolved.method is method


def test_apply_url_never_returns_empty() -> None:
    """A posting with no apply link is not a posting; source_url is the floor."""
    assert resolve_apply_url(ats_native="  ", source_url="https://s/1").url == "https://s/1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://careers.vodafone.com.eg/jobs", "vodafone.com.eg"),
        ("https://jobs.example.co.uk", "example.co.uk"),
        ("boards.greenhouse.io", "greenhouse.io"),
        ("localhost", None),
    ],
)
def test_registrable_domain(value: str, expected: str | None) -> None:
    assert registrable_domain(value) == expected


def test_url_host_drops_www() -> None:
    assert url_host("https://www.Example.com/x") == "example.com"


# ── Identity-bearing parameters ───────────────────────────────────────
#
# Regression: `gh_jid` was treated as a tracking parameter. Greenhouse boards
# rendered on an employer's own domain carry the job id there, so stripping it
# turned 870 distinct Databricks postings into one URL — which deduplication
# then merged into a single group.


def test_gh_jid_is_identity_and_survives() -> None:
    url = "https://databricks.com/company/careers/open-positions/job?gh_jid=7907944002"
    assert "gh_jid=7907944002" in canonicalize_url(url)
    assert "gh_jid=7907944002" in resolve_apply_url(ats_native=url, source_url="https://s/1").url


def test_apply_url_keeps_parameters_that_might_carry_identity() -> None:
    """`ref`, `source` and friends are stripped from the comparison key only.

    A parameter that looks like tracking to us may be the posting's identity to
    the employer, and a broken apply link is the most visible defect we can ship.
    """
    url = "https://jobs.example.com/1?ref=partner&source=board&utm_source=x&gclid=y"
    apply = resolve_apply_url(ats_native=url, source_url="https://s/1").url
    assert "ref=partner" in apply and "source=board" in apply
    assert "utm_source" not in apply and "gclid" not in apply

    key = canonicalize_url(apply)
    assert "ref=" not in key and "source=" not in key


def test_two_jobs_on_one_board_do_not_share_a_comparison_key() -> None:
    a = "https://databricks.com/careers/job?gh_jid=1"
    b = "https://databricks.com/careers/job?gh_jid=2"
    assert canonicalize_url(a) != canonicalize_url(b)
