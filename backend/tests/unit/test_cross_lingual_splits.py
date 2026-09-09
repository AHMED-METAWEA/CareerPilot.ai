"""Cross-lingual evaluation splits (§18, Phase 5).

The exit criterion is a comparison between two numbers, so the thing worth
testing hardest is what happens when one of them does not exist. A split with no
labels must not read as a bad score, and the criterion must not silently pass or
silently fail while the Arabic golden set is unlabelled.
"""

from __future__ import annotations

from app.eval.splits import (
    EXIT_CRITERION_POINTS,
    MIN_PAIRS_FOR_A_NUMBER,
    evaluate_by_language,
)


def _pairs(prefix: str, count: int, *, grade: int = 3) -> dict[str, int]:
    return {f"{prefix}{index}": grade for index in range(count)}


def _languages(prefix: str, count: int, language: str) -> dict[str, str]:
    return {f"{prefix}{index}": language for index in range(count)}


def test_a_split_with_no_labels_has_no_number() -> None:
    """Not zero. Zero is a measurement, and this is the absence of one."""
    labels = {"p1": _pairs("en", 30)}
    report = evaluate_by_language(
        {"p1": [f"en{index}" for index in range(30)]},
        labels,
        posting_languages=_languages("en", 30, "en"),
        profile_languages={"p1": "en"},
    )

    arabic = report.arabic
    assert arabic is not None
    assert arabic.pairs == 0
    assert arabic.ndcg is None
    assert not arabic.is_measurable


def test_the_exit_criterion_is_none_while_the_arabic_split_is_unlabelled() -> None:
    """Neither met nor missed — the state the project is actually in."""
    labels = {"p1": _pairs("en", 30)}
    report = evaluate_by_language(
        {"p1": [f"en{index}" for index in range(30)]},
        labels,
        posting_languages=_languages("en", 30, "en"),
        profile_languages={"p1": "en"},
    )

    assert report.gap_points is None
    assert report.meets_exit_criterion is None
    assert "neither met nor missed" in report.markdown_table()


def test_a_thin_split_is_reported_as_thin_not_as_a_score() -> None:
    """A handful of pairs produces a number that looks like a measurement."""
    thin = MIN_PAIRS_FOR_A_NUMBER - 1
    labels = {"p1": _pairs("en", 30), "p2": _pairs("ar", thin)}
    rankings = {
        "p1": [f"en{index}" for index in range(30)],
        "p2": [f"ar{index}" for index in range(thin)],
    }
    report = evaluate_by_language(
        rankings,
        labels,
        posting_languages=_languages("en", 30, "en") | _languages("ar", thin, "ar"),
        profile_languages={"p1": "en", "p2": "ar"},
    )

    arabic = report.arabic
    assert arabic is not None
    assert arabic.pairs == thin
    assert arabic.metrics is not None, "the metric is computed…"
    assert arabic.ndcg is None, "…but not published as a measurement"
    assert report.meets_exit_criterion is None
    assert f"only {thin} pairs" in report.markdown_table()


def test_two_measurable_splits_produce_a_signed_gap() -> None:
    count = MIN_PAIRS_FOR_A_NUMBER + 5
    labels = {"p1": _pairs("en", count), "p2": _pairs("ar", count)}
    rankings = {
        "p1": [f"en{index}" for index in range(count)],
        # The Arabic profile's ranking is reversed relative to its labels, but
        # all grades are equal, so both splits rank perfectly and the gap is 0.
        "p2": [f"ar{index}" for index in reversed(range(count))],
    }
    report = evaluate_by_language(
        rankings,
        labels,
        posting_languages=_languages("en", count, "en") | _languages("ar", count, "ar"),
        profile_languages={"p1": "en", "p2": "ar"},
    )

    assert report.gap_points == 0.0
    assert report.meets_exit_criterion is True
    assert "within the 5-point exit criterion" in report.markdown_table()


def test_a_wide_gap_fails_the_criterion() -> None:
    count = MIN_PAIRS_FOR_A_NUMBER + 5
    # English ranks its graded postings first; Arabic buries them under
    # zero-graded ones, which is what a broken cross-lingual retrieval looks like.
    english_labels = _pairs("en", count)
    arabic_labels = _pairs("ar", count) | {f"arz{index}": 0 for index in range(count)}
    rankings = {
        "p1": [f"en{index}" for index in range(count)],
        "p2": [f"arz{index}" for index in range(count)] + [f"ar{index}" for index in range(count)],
    }
    report = evaluate_by_language(
        rankings,
        {"p1": english_labels, "p2": arabic_labels},
        posting_languages=(
            _languages("en", count, "en")
            | _languages("ar", count, "ar")
            | _languages("arz", count, "ar")
        ),
        profile_languages={"p1": "en", "p2": "ar"},
    )

    gap = report.gap_points
    assert gap is not None and gap > EXIT_CRITERION_POINTS
    assert report.meets_exit_criterion is False
    assert "outside the 5-point exit criterion" in report.markdown_table()


def test_the_cross_lingual_cells_are_reported_separately() -> None:
    """ar→ar can be strong while ar→en is broken; pooling hides exactly that."""
    count = MIN_PAIRS_FOR_A_NUMBER + 5
    labels = {"p_ar": _pairs("en", count) | _pairs("ar", count)}
    rankings = {"p_ar": [f"ar{i}" for i in range(count)] + [f"en{i}" for i in range(count)]}
    report = evaluate_by_language(
        rankings,
        labels,
        posting_languages=_languages("en", count, "en") | _languages("ar", count, "ar"),
        profile_languages={"p_ar": "ar"},
    )

    names = {split.name for split in report.cross_lingual}
    assert names == {"ar→en", "en→ar"}
    assert {split.name for split in report.splits} == {"en→en", "en→ar", "ar→en", "ar→ar"}

    cross = report.by_name("ar→en")
    assert cross is not None and cross.pairs == count


def test_a_cell_restricts_the_ranking_as_well_as_the_labels() -> None:
    """Otherwise unlabelled postings hold the top slots and depress every split."""
    count = MIN_PAIRS_FOR_A_NUMBER + 5
    labels = {"p1": _pairs("ar", count)}
    # Every English posting outranks every Arabic one. If the Arabic cell kept
    # them in its ranking, the graded postings would start at position 26.
    rankings = {"p1": [f"en{i}" for i in range(count)] + [f"ar{i}" for i in range(count)]}
    report = evaluate_by_language(
        rankings,
        labels,
        posting_languages=_languages("en", count, "en") | _languages("ar", count, "ar"),
        profile_languages={"p1": "ar"},
    )

    arabic = report.arabic
    assert arabic is not None and arabic.ndcg == 1.0
