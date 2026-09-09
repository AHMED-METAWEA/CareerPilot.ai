# ADR 0006 — Percentile presentation over absolute scores

**Status:** accepted · **Date:** 2026-09-08 · **Implemented:** Phase 1

## Context

A match score shown as "78%" invites exactly one reading: a 78% chance of
getting the job. That reading is statistically indefensible and actively
misleading (§2.4).

## Decision

Present a match as a percentile within the candidate's own reviewed pool —
*"top 4% of the 1,240 roles reviewed for you this week"* — never as an absolute
figure, and never as a probability of any outcome.

## Consequences

- Self-calibrating: as the corpus grows or the scorer changes, the presentation
  stays meaningful without recalibrating a number in the user's head.
- Resistant to score inflation, which otherwise happens silently when the
  corpus shifts under a fixed threshold.
- The absolute score and every sub-score remain available in the API and in the
  evidence view, for users who want the decomposition.
