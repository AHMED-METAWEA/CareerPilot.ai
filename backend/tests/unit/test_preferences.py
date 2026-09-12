"""Preference learning (§11.8, §18 Phase 6).

The fit is the easy part. What is worth testing hard is every constraint §11.8
places around it — the bound, the sum, the refusal to learn from views, and the
position-bias control — because those are what stop a personalised ranking from
quietly becoming an unaccountable one.
"""

from __future__ import annotations

import random

import pytest

from app.domain.scoring.aggregate import ScoreWeights
from app.domain.scoring.preferences import (
    FEATURES,
    MIN_EVENTS,
    WEIGHT_BOUND,
    Coefficients,
    NotEnoughSignal,
    TrainingExample,
    build_examples,
    fit_logistic,
    personalised_weights,
)


def synthetic(
    count: int, *, driver: str, noise: float = 0.0, seed: int = 7
) -> list[TrainingExample]:
    """Examples where exactly one sub-score decides the label.

    A user who only ever engages with one kind of posting is the clearest case
    a preference model can be asked about; if the fit cannot recover that, it
    cannot recover anything subtler.
    """
    rng = random.Random(seed)
    examples: list[TrainingExample] = []
    for index in range(count):
        subscores = {name: rng.random() for name in FEATURES}
        label = 1 if subscores[driver] > 0.5 else 0
        if noise and rng.random() < noise:
            label = 1 - label
        examples.append(TrainingExample(subscores=subscores, label=label, rank=index % 25 + 1))
    return examples


# ── Refusing to fit ───────────────────────────────────────────────────


def test_a_fit_needs_both_classes() -> None:
    """All-positive data produces "everything is good", which is not a preference."""
    examples = [
        TrainingExample(subscores=dict.fromkeys(FEATURES, 0.8), label=1) for _ in range(300)
    ]
    with pytest.raises(NotEnoughSignal, match="negative"):
        fit_logistic(examples)


def test_a_handful_of_dismissals_is_not_a_preference() -> None:
    examples = synthetic(300, driver="freshness")[:200]
    trimmed = [e for e in examples if e.label == 1][:150]
    trimmed += [e for e in examples if e.label == 0][:3]
    with pytest.raises(NotEnoughSignal):
        fit_logistic(trimmed)


def test_the_activation_gate_is_the_number_the_plan_states() -> None:
    assert MIN_EVENTS == 200, "§11.8 activates personalisation above 200 events"


# ── The fit ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("driver", FEATURES)
def test_the_fit_recovers_the_term_that_decided_the_label(driver: str) -> None:
    coefficients = fit_logistic(synthetic(400, driver=driver))
    assert coefficients.strongest() == driver
    assert coefficients.values[driver] > 0


def test_the_fit_is_deterministic() -> None:
    """Same events, same coefficients — or a personalised list cannot be explained."""
    examples = synthetic(300, driver="skill_coverage")
    assert fit_logistic(examples).values == fit_logistic(examples).values


def test_a_dismissed_term_gets_a_negative_coefficient() -> None:
    """Someone who dismisses everything stale should not be given stale postings."""
    rng = random.Random(3)
    examples = []
    for index in range(400):
        subscores = {name: rng.random() for name in FEATURES}
        # Label is *inverted* on freshness: this user engages with older,
        # more-established postings and dismisses the newest ones.
        label = 1 if subscores["freshness"] < 0.5 else 0
        examples.append(TrainingExample(subscores=subscores, label=label, rank=index % 20 + 1))

    coefficients = fit_logistic(examples)
    assert coefficients.values["freshness"] < 0


def test_the_fit_survives_noisy_labels() -> None:
    """Real engagement is not clean; a fit that only works on clean data is a toy."""
    coefficients = fit_logistic(synthetic(600, driver="requirement_alignment", noise=0.15))
    assert coefficients.strongest() == "requirement_alignment"


def test_extreme_scores_do_not_overflow_the_sigmoid() -> None:
    """One inf turns every subsequent gradient into nan."""
    rng = random.Random(11)
    examples = [
        TrainingExample(
            subscores=dict.fromkeys(FEATURES, 1.0 if index % 2 else 0.0),
            label=index % 2,
            rank=rng.randint(1, 50),
        )
        for index in range(200)
    ]
    coefficients = fit_logistic(examples, learning_rate=5.0, max_iterations=500)
    assert all(value == value for value in coefficients.values.values())  # not nan
    assert coefficients.log_loss == coefficients.log_loss


# ── Position bias ─────────────────────────────────────────────────────


def test_rank_is_fitted_as_a_control_and_never_becomes_a_weight() -> None:
    """People save what is near the top because it is near the top (§11.8).

    When engagement is driven purely by position, the control should absorb it
    and the sub-score coefficients should stay small — so the weights barely
    move rather than encoding the ranking's own output as a preference.
    """
    rng = random.Random(5)
    examples = [
        TrainingExample(
            subscores={name: rng.random() for name in FEATURES},
            label=1 if rank <= 5 else 0,
            rank=rank,
        )
        for _ in range(80)
        for rank in range(1, 11)
    ]

    coefficients = fit_logistic(examples)
    strongest_subscore = max(abs(value) for value in coefficients.values.values())
    assert abs(coefficients.rank_control) > strongest_subscore, (
        "position bias must land on the control, not on the sub-scores"
    )
    assert "rank" not in coefficients.values, "rank must never become a weight"

    _, adjustments = personalised_weights(coefficients)
    assert max(abs(value) for value in adjustments.values()) < WEIGHT_BOUND


# ── The ±40% bound and the simplex ────────────────────────────────────


@pytest.mark.parametrize("driver", FEATURES)
def test_weights_stay_within_the_bound_and_sum_to_one(driver: str) -> None:
    """§11.8's two hard constraints, together — they fight each other."""
    weights, adjustments = personalised_weights(fit_logistic(synthetic(400, driver=driver)))
    defaults = ScoreWeights()

    assert weights.total() == pytest.approx(1.0, abs=1e-9)
    for name in FEATURES:
        ratio = getattr(weights, name) / getattr(defaults, name)
        assert 1.0 - WEIGHT_BOUND - 1e-9 <= ratio <= 1.0 + WEIGHT_BOUND + 1e-9, (
            f"{name} moved {ratio:.3f}× its default, outside ±{WEIGHT_BOUND:.0%}"
        )
        assert abs(adjustments[name]) <= WEIGHT_BOUND + 1e-9


def test_an_extreme_fit_is_capped_not_obeyed() -> None:
    """A user whose events say "only freshness" still gets skills weighed."""
    extreme = Coefficients(
        values={name: (50.0 if name == "freshness" else -50.0) for name in FEATURES}
    )
    weights, adjustments = personalised_weights(extreme)
    defaults = ScoreWeights()

    assert weights.total() == pytest.approx(1.0, abs=1e-9)
    assert weights.skill_coverage >= defaults.skill_coverage * (1 - WEIGHT_BOUND) - 1e-9
    assert weights.skill_coverage > weights.freshness, (
        "skills still outweigh freshness: the default encodes what the product is for"
    )
    assert adjustments["freshness"] > 0 > adjustments["skill_coverage"]


def test_a_flat_fit_returns_the_defaults_untouched() -> None:
    """No signal means no change, not a random nudge."""
    weights, adjustments = personalised_weights(Coefficients(values=dict.fromkeys(FEATURES, 0.0)))
    assert weights == ScoreWeights()
    assert set(adjustments.values()) == {0.0}


def test_the_projection_terminates_on_pathological_input() -> None:
    """Every term pushed to a limit at once still has to produce a valid simplex."""
    for sign in (1.0, -1.0):
        weights, _ = personalised_weights(Coefficients(values=dict.fromkeys(FEATURES, sign * 99.0)))
        assert weights.total() == pytest.approx(1.0, abs=1e-9)


# ── What counts as a signal ───────────────────────────────────────────


def test_views_are_not_preference() -> None:
    """A view is what the ranking showed, not what the candidate chose (§11.8)."""
    rows = [
        (dict.fromkeys(FEATURES, 0.5), "viewed", 1),
        (dict.fromkeys(FEATURES, 0.5), "saved", 2),
        (dict.fromkeys(FEATURES, 0.5), "dismissed", 3),
        (dict.fromkeys(FEATURES, 0.5), "applied", 4),
    ]
    examples = build_examples(rows)

    assert len(examples) == 3, "viewed must be dropped"
    assert [example.label for example in examples] == [1, 0, 1]


def test_applying_and_saving_are_both_positive() -> None:
    rows = [(dict.fromkeys(FEATURES, 0.9), event, None) for event in ("saved", "applied")]
    assert [example.label for example in build_examples(rows)] == [1, 1]


def test_an_unknown_event_is_ignored_rather_than_guessed() -> None:
    assert build_examples([({}, "shared_on_linkedin", None)]) == []
