"""The measurement behind `dedup.simhash_hamming_max` (§11.3 stage 4).

These assertions are the evidence for the configured threshold. If a future
change to tokenisation, shingling or hashing moves these distances, the
threshold has to be re-derived rather than the test relaxed.
"""

from __future__ import annotations

import re

import pytest

from app.config import get_config
from app.domain.dedup.simhash import hamming, simhash64
from app.domain.jobs.normalize import html_to_text
from tests.conftest import load_fixture

TRAILER = (
    "\n\nApply now through our careers portal. We are an equal opportunity employer "
    "and welcome applicants from every background."
)


@pytest.fixture(scope="module")
def descriptions() -> list[str]:
    texts = [
        html_to_text(job["content"]) for job in load_fixture("greenhouse", "board.json")["jobs"]
    ]
    ashby = load_fixture("ashby", "board.json")["jobs"]
    texts += [
        job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", "")) for job in ashby
    ]
    long_enough = [t for t in texts if len(t) > 800]
    assert len(long_enough) >= 3, "fixtures no longer contain full-length descriptions"
    return long_enough


def threshold() -> int:
    return get_config().dedup.simhash_hamming_max


def test_syndication_trailer_stays_within_threshold(descriptions: list[str]) -> None:
    for text in descriptions:
        assert hamming(simhash64(text), simhash64(text + TRAILER)) <= threshold()


def test_whitespace_changes_do_not_move_the_hash(descriptions: list[str]) -> None:
    for text in descriptions:
        assert hamming(simhash64(text), simhash64(re.sub(r"\s+", " ", text))) == 0


def test_different_postings_are_far_outside_the_threshold(descriptions: list[str]) -> None:
    distances = [
        hamming(simhash64(a), simhash64(b))
        for i, a in enumerate(descriptions)
        for b in descriptions[i + 1 :]
    ]
    assert min(distances) > threshold() + 4, (
        "the margin between 'same posting' and 'different posting' has collapsed; "
        f"closest unrelated pair is {min(distances)} bits"
    )


def test_heavily_truncated_copies_are_a_known_miss(descriptions: list[str]) -> None:
    """Documented limitation, asserted so it stays documented.

    An aggregator that publishes 85% of a description lands 5–11 bits away —
    inside the range where unrelated postings also live. SimHash does not catch
    these, and the threshold is not widened to try: the exact-key, canonical-URL
    and title-blocking stages catch them without risking a false merge.
    """
    misses = 0
    for text in descriptions:
        truncated = text[: int(len(text) * 0.85)]
        if hamming(simhash64(text), simhash64(truncated)) > threshold():
            misses += 1
    assert misses > 0
