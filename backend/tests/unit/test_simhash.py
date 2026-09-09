"""SimHash-64 near-duplicate detection (§11.3 stage 4)."""

from __future__ import annotations

from app.domain.dedup.simhash import (
    from_signed,
    hamming,
    is_near_duplicate,
    shingles,
    simhash64,
    to_signed,
    tokenize,
)

SHORT = (
    "We are looking for a senior backend engineer to join our payments team in Cairo. "
    "You will design and operate distributed services in Python and Go, own reliability "
    "for a high-throughput ledger, and mentor engineers across two squads. We offer "
    "hybrid working, private medical cover and an annual learning budget."
)
# A real job description runs to several hundred words. Length matters to this
# algorithm, so the fixtures reflect it.
BASE = SHORT * 6


def test_identical_text_hashes_identically() -> None:
    assert simhash64(BASE) == simhash64(BASE)


def test_boilerplate_difference_stays_within_threshold() -> None:
    """The same posting syndicated twice differs only in trailer text."""
    syndicated = BASE + " Apply now through our careers portal. Recruiters: no agencies."
    assert is_near_duplicate(simhash64(BASE), simhash64(syndicated), max_distance=3)


def test_threshold_is_only_meaningful_at_full_description_length() -> None:
    """Measured limitation, not a defect: the distance tracks the *share* of the
    document that changed. The same trailer on a 60-word stub moves the hash far
    more than on a 350-word description, which is why clustering does not trust
    SimHash below `MIN_SIMHASH_CHARS`."""
    trailer = " Apply now through our careers portal. Recruiters: no agencies."
    assert hamming(simhash64(BASE), simhash64(BASE + trailer)) <= 3
    assert hamming(simhash64(SHORT), simhash64(SHORT + trailer)) > 3


def test_different_roles_are_far_apart() -> None:
    other = (
        "We are hiring a registered nurse for our paediatric ward in Alexandria. "
        "You will deliver patient care, administer medication and support families "
        "through treatment plans. Shift work, including nights and weekends."
    )
    assert hamming(simhash64(BASE), simhash64(other)) > 3


def test_signed_round_trip_is_lossless() -> None:
    """Postgres has no unsigned bigint; the conversion must not lose the hash."""
    value = simhash64(BASE)
    signed = to_signed(value)
    assert -(2**63) <= signed < 2**63
    assert from_signed(signed) == value


def test_hamming_handles_signed_inputs() -> None:
    a, b = simhash64(BASE), simhash64(BASE + " extra")
    assert hamming(to_signed(a), to_signed(b)) == hamming(a, b)


def test_empty_text_hashes_to_zero() -> None:
    assert simhash64("") == 0


def test_short_text_still_produces_one_shingle() -> None:
    assert shingles(tokenize("two words"), 5) == ["two words"]
