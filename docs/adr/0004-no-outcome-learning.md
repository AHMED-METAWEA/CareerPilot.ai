# ADR 0004 — No outcome-based learning-to-rank

**Status:** accepted · **Date:** 2026-09-08

## Context

The apparently obvious machine-learning move is to train a ranker on
application outcomes: did the user get an interview, an offer, a rejection.

## Decision

Do not train on application outcomes. Personalisation (Phase 6) uses implicit
engagement signals — save, dismiss, view — through an interpretable model over
existing sub-scores, bounded to ±40% of default weights and validated per user.

## Consequences

Outcome labels are:

- **sparse** — a user applies to a handful of roles;
- **censored** — ghosting is the majority outcome, and "no response" is not
  "rejected";
- **delayed** — weeks between action and label;
- **confounded** — referrals, timing, headcount freezes and competition drive
  outcomes far more than fit does.

A model trained on them would learn noise and present it with false confidence,
which is precisely the failure this product exists to avoid. The rule that
follows from the same reasoning is enforced in the UI: a match score is never
described as a probability of being hired or interviewed (§8.5).
