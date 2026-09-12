"""Preference learning (§11.8, §18 Phase 6).

A small logistic model over the sub-scores the system already computes, used to
nudge one user's weights toward what they actually engage with. Three
constraints from §11.8 shape every decision here, and none of them is optional:

* **Interpretable coefficients only.** Five features, one coefficient each, each
  naming a term the evidence view already displays. A candidate can be shown
  why their list changed, in the same vocabulary the list is scored in.
* **Bounded to ±40% of the defaults.** Personalisation adjusts a ranking; it
  does not replace it. A user whose events say "only freshness matters" still
  gets a list that weighs skills, because the defaults encode what the product
  is for and one user's clicking is a weaker signal than that.
* **Validated before use.** Fitting a model is not evidence that it helps. The
  caller must show improvement on that user's own labelled subset before the
  weights are applied — see `app.services.personalisation`.

Two things this deliberately does not learn from:

**Outcomes.** ADR 0004 and §11.8: application outcomes are sparse, censored by
non-response, delayed by weeks and confounded by referrals and timing. Training
on them would learn noise and present it with confidence.

**Views.** A view is what the *ranking* showed the user, not what the user
wanted. Training on views teaches the model to reproduce its own output, and the
loop tightens every run. Only a deliberate act — saving, applying, dismissing —
counts as preference.

The confound that remains is position bias: people save what is near the top
because it is near the top. It is handled by fitting rank as a control feature
and then discarding its coefficient, so rank explains what it can and the
sub-score coefficients carry only what is left.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.scoring.aggregate import ScoreWeights

MIN_EVENTS = 200
"""§11.8: personalisation activates only above this many engagement events.

Not a tuning knob. Below a few hundred deliberate acts, a five-parameter fit is
describing noise, and the cost of being wrong is a worse list for the person
least able to tell that it got worse."""

WEIGHT_BOUND = 0.40
"""§11.8: no weight may move more than this fraction from its default."""

REFERENCE_EFFECT = 2.0
"""The coefficient magnitude treated as a strong effect when scaling a fit
into a weight adjustment. On a 0–1 feature, β = 2 multiplies the odds by
about seven across the feature's range."""

FEATURES: tuple[str, ...] = (
    "skill_coverage",
    "requirement_alignment",
    "seniority_fit",
    "semantic_similarity",
    "freshness",
)

MIN_POSITIVES = 20
MIN_NEGATIVES = 20
"""A fit needs both classes. All-positive data produces a model that says
"everything is good", which is not a preference."""


class NotEnoughSignal(ValueError):
    """Refuse to fit rather than fit something meaningless."""


@dataclass(frozen=True, slots=True)
class TrainingExample:
    """One engagement, as the model sees it."""

    subscores: dict[str, float]
    label: int
    """1 for a deliberate positive (saved, applied), 0 for a dismissal."""
    rank: int | None = None
    """Position in the list the user was shown. A control, never a weight."""


@dataclass(frozen=True, slots=True)
class Coefficients:
    """The fitted model, in the vocabulary the evidence view already uses."""

    values: dict[str, float]
    intercept: float = 0.0
    rank_control: float = 0.0
    """How much of the engagement rank alone explained. Reported, never applied:
    a large value here is a warning that the sub-score coefficients are
    describing where things sat on the page."""
    iterations: int = 0
    converged: bool = False
    log_loss: float = 0.0

    def strongest(self) -> str | None:
        if not self.values:
            return None
        return max(self.values, key=lambda key: abs(self.values[key]))


@dataclass(slots=True)
class FitReport:
    """Everything needed to explain a fit to the person it describes."""

    coefficients: Coefficients
    examples: int = 0
    positives: int = 0
    negatives: int = 0
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    adjustments: dict[str, float] = field(default_factory=dict)
    """Per-term change as a fraction of the default, e.g. +0.18 for +18%."""

    def as_dict(self) -> dict[str, object]:
        return {
            "examples": self.examples,
            "positives": self.positives,
            "negatives": self.negatives,
            "coefficients": self.coefficients.values,
            "rank_control": self.coefficients.rank_control,
            "converged": self.coefficients.converged,
            "log_loss": round(self.coefficients.log_loss, 5),
            "weights": {name: getattr(self.weights, name) for name in FEATURES},
            "adjustments": self.adjustments,
        }


def _sigmoid(z: float) -> float:
    # Split on the sign so neither branch can overflow: exp(710) is inf, and a
    # single inf turns the whole gradient into nan for every subsequent step.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def fit_logistic(
    examples: Sequence[TrainingExample],
    *,
    l2: float = 1.0,
    learning_rate: float = 0.5,
    max_iterations: int = 2000,
    tolerance: float = 1e-7,
) -> Coefficients:
    """Fit a logistic model over the sub-scores, with rank as a control.

    Plain batch gradient descent, initialised at zero. Deterministic by
    construction: the same events give the same coefficients, which is what
    makes a personalised ranking explainable after the fact.

    The L2 term is doing real work, not decoration. Sub-scores are correlated —
    a posting that matches on skills usually matches on requirements too — and
    unregularised logistic regression on collinear features produces large
    coefficients of opposite sign that cancel. Those are unstable and
    uninterpretable, which fails §11.8's first constraint before the numbers
    are even used.
    """
    positives = sum(1 for example in examples if example.label == 1)
    negatives = len(examples) - positives
    if positives < MIN_POSITIVES or negatives < MIN_NEGATIVES:
        raise NotEnoughSignal(
            f"need at least {MIN_POSITIVES} positive and {MIN_NEGATIVES} negative "
            f"examples; have {positives} and {negatives}"
        )

    ranks = [example.rank for example in examples if example.rank is not None]
    max_rank = max(ranks) if ranks else 0

    rows: list[tuple[list[float], float, int]] = []
    for example in examples:
        features = [float(example.subscores.get(name, 0.0)) for name in FEATURES]
        # Normalised so the control is on the same 0–1 scale as the sub-scores
        # and its coefficient is comparable to theirs. Unknown rank sits at the
        # midpoint rather than at the top, which would flatter the example.
        rank_feature = example.rank / max_rank if max_rank > 0 and example.rank is not None else 0.5
        rows.append((features, rank_feature, example.label))

    weights = [0.0] * len(FEATURES)
    rank_weight = 0.0
    intercept = 0.0
    count = len(rows)
    previous_loss = math.inf
    converged = False
    iteration = 0

    for step in range(1, max_iterations + 1):
        iteration = step
        gradient = [0.0] * len(FEATURES)
        rank_gradient = 0.0
        intercept_gradient = 0.0
        loss = 0.0

        for features, rank_feature, label in rows:
            z = intercept + rank_weight * rank_feature
            for index, value in enumerate(features):
                z += weights[index] * value
            prediction = _sigmoid(z)
            error = prediction - label

            for index, value in enumerate(features):
                gradient[index] += error * value
            rank_gradient += error * rank_feature
            intercept_gradient += error

            # Clamped so a confident wrong prediction contributes a large but
            # finite loss instead of inf, which would make the convergence test
            # meaningless.
            probability = prediction if label == 1 else 1.0 - prediction
            loss -= math.log(max(probability, 1e-12))

        loss = loss / count + l2 * sum(value * value for value in weights) / (2 * count)

        for index in range(len(FEATURES)):
            gradient[index] = gradient[index] / count + l2 * weights[index] / count
            weights[index] -= learning_rate * gradient[index]
        rank_weight -= learning_rate * (rank_gradient / count + l2 * rank_weight / count)
        intercept -= learning_rate * intercept_gradient / count

        if abs(previous_loss - loss) < tolerance:
            converged = True
            previous_loss = loss
            break
        previous_loss = loss

    return Coefficients(
        values=dict(zip(FEATURES, weights, strict=True)),
        intercept=intercept,
        rank_control=rank_weight,
        iterations=iteration,
        converged=converged,
        log_loss=previous_loss if previous_loss != math.inf else 0.0,
    )


def personalised_weights(
    coefficients: Coefficients,
    defaults: ScoreWeights | None = None,
    *,
    bound: float = WEIGHT_BOUND,
) -> tuple[ScoreWeights, dict[str, float]]:
    """Turn coefficients into weights that sum to 1.0 and respect the bound.

    The direction comes from the model; the magnitude is capped by the product.

    Each coefficient is squashed through `tanh(β / REFERENCE_EFFECT)`, which is
    what keeps a weak fit weak. Normalising by the *largest* coefficient instead
    — the obvious first approach — makes every fit saturate the bound: a user
    whose engagement was pure position bias, whose coefficients are all near
    zero, would still have their weights moved the full 40% in whatever
    direction the noise happened to point. Squashing on a fixed scale means a
    coefficient near zero moves the weight near zero, and only a genuinely
    strong effect reaches the ceiling.

    `REFERENCE_EFFECT` is the coefficient magnitude treated as a strong signal.
    On a 0–1 feature, β = 2 is an odds ratio of about 7 across the range, which
    is a large effect for a sub-score that was already predictive enough to ship.

    Returns the weights and the per-term adjustment as a fraction of default.
    """
    base = defaults or ScoreWeights()
    default_map = {name: getattr(base, name) for name in FEATURES}

    if not any(coefficients.values.values()):
        return base, dict.fromkeys(FEATURES, 0.0)

    multipliers = {
        name: 1.0 + bound * math.tanh(coefficients.values.get(name, 0.0) / REFERENCE_EFFECT)
        for name in FEATURES
    }
    raw = {name: default_map[name] * multipliers[name] for name in FEATURES}
    projected = _project_to_simplex(raw, default_map, bound=bound)

    adjustments = {
        name: round(projected[name] / default_map[name] - 1.0, 4) if default_map[name] else 0.0
        for name in FEATURES
    }
    return ScoreWeights(**projected), adjustments


def _project_to_simplex(
    raw: dict[str, float], defaults: dict[str, float], *, bound: float
) -> dict[str, float]:
    """Scale to sum 1.0 while keeping every term within ±`bound` of its default.

    Normalising and clamping fight each other: clamping breaks the sum, and
    renormalising breaks the clamp. This moves the whole deficit or surplus in
    one pass, distributed in proportion to how much room each term has left in
    the direction of travel.

    A feasible point always exists, which is why this can be exact rather than
    iterative: the defaults sum to 1.0, so the reachable range of the sum is
    [1−bound, 1+bound], and 1.0 sits inside it.

    The direction matters, and getting it wrong was a real bug. When every term
    starts pinned — one at its ceiling and the rest at their floor, which is
    exactly what an extreme fit produces — the terms at the floor are the only
    ones with room to move *up*. Freeing only terms that are strictly inside
    their bounds leaves nothing free at all, and the sum stays at 0.68.
    """
    lower = {name: value * (1.0 - bound) for name, value in defaults.items()}
    upper = {name: value * (1.0 + bound) for name, value in defaults.items()}
    weights = {name: min(upper[name], max(lower[name], value)) for name, value in raw.items()}

    surplus = sum(weights.values()) - 1.0
    if abs(surplus) < 1e-15:
        return weights

    # Headroom in the direction we need to travel, never the other way.
    if surplus > 0:
        capacity = {name: weights[name] - lower[name] for name in weights}
    else:
        capacity = {name: upper[name] - weights[name] for name in weights}

    available = sum(capacity.values())
    if available <= 0:
        return weights

    movement = min(abs(surplus), available)
    direction = -1.0 if surplus > 0 else 1.0
    for name in weights:
        weights[name] += direction * movement * capacity[name] / available

    # Floating-point drift must never be the reason a published weight sits
    # outside the bound the product states.
    return {name: min(upper[name], max(lower[name], value)) for name, value in weights.items()}


def build_examples(
    rows: Sequence[tuple[dict[str, float], str, int | None]],
) -> list[TrainingExample]:
    """Turn (subscores, event, rank) rows into labelled examples.

    `viewed` is dropped here rather than filtered by the caller, so that the
    rule lives next to the reasoning for it: a view is what the ranking chose to
    show, and training on it closes a loop around the system's own output.
    """
    positive = {"saved", "applied"}
    negative = {"dismissed"}
    examples: list[TrainingExample] = []
    for subscores, event, rank in rows:
        if event in positive:
            label = 1
        elif event in negative:
            label = 0
        else:
            continue
        examples.append(TrainingExample(subscores=subscores, label=label, rank=rank))
    return examples
