"""ATS detection precedence (Appendix C)."""

from __future__ import annotations

import pytest

from app.domain.jobs.ats_detect import detect, detect_from_html, detect_from_url
from app.domain.models import ATSPlatform, DetectionMethod


def test_construction_beats_everything() -> None:
    """A posting fetched from an ATS's own API is on that ATS. Confidence 1.00."""
    found = detect(
        constructed=ATSPlatform.LEVER,
        url="https://boards.greenhouse.io/acme/jobs/1",
        html="<div id='grnhse_app'></div>",
    )
    assert (found.platform, found.confidence, found.method) == (
        ATSPlatform.LEVER,
        1.00,
        DetectionMethod.CONSTRUCTION,
    )


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://boards.greenhouse.io/acme/jobs/1", ATSPlatform.GREENHOUSE),
        ("https://job-boards.greenhouse.io/acme/jobs/1", ATSPlatform.GREENHOUSE),
        ("https://jobs.lever.co/acme/1", ATSPlatform.LEVER),
        ("https://jobs.ashbyhq.com/acme/1", ATSPlatform.ASHBY),
        ("https://apply.workable.com/acme/j/ABC/", ATSPlatform.WORKABLE),
        ("https://acme.recruitee.com/o/engineer", ATSPlatform.RECRUITEE),
        ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1", ATSPlatform.WORKDAY),
        ("https://careers.acme.com/jobs/1", ATSPlatform.UNKNOWN),
    ],
)
def test_url_patterns(url: str, platform: ATSPlatform) -> None:
    assert detect_from_url(url).platform is platform


def test_url_confidence_is_095_and_redirect_is_075() -> None:
    assert detect_from_url("https://jobs.lever.co/acme/1").confidence == 0.95
    found = detect(redirect_terminus="https://jobs.lever.co/acme/1")
    assert (found.confidence, found.method) == (0.75, DetectionMethod.REDIRECT)


def test_bamboohr_requires_a_careers_path() -> None:
    """The host alone serves far more than recruiting; the path is the evidence."""
    assert detect_from_url("https://acme.bamboohr.com/careers/42").platform is ATSPlatform.BAMBOOHR
    assert detect_from_url("https://acme.bamboohr.com/login").platform is ATSPlatform.UNKNOWN


def test_html_fingerprint() -> None:
    found = detect_from_html('<div id="grnhse_app"></div>')
    assert (found.platform, found.confidence, found.method) == (
        ATSPlatform.GREENHOUSE,
        0.80,
        DetectionMethod.FINGERPRINT,
    )


def test_unknown_is_a_valid_answer() -> None:
    """Nothing is guessed: an unresolved posting is honestly 'unknown' (Appendix C row 5)."""
    found = detect(url="https://careers.acme.com/1", html="<html><body>Apply</body></html>")
    assert found.platform is ATSPlatform.UNKNOWN
    assert found.confidence == 0.0
