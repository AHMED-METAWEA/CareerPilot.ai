# ADR 0003 — Decomposed scoring over end-to-end similarity

**Status:** accepted · **Date:** 2026-09-08 · **Implemented:** Phase 1

## Context

The obvious approach — cosine similarity between a whole-CV embedding and a
whole-JD embedding — is one line of code and largely useless: it is dominated by
topical overlap, compresses real differences into a 0.60–0.85 band, is blind to
hard disqualifiers, and cannot be explained to a user.

## Decision

Score as a weighted sum of named sub-scores behind boolean gates:

```
gate  = Π gate_i
score = gate × (0.35·skill_coverage + 0.25·requirement_alignment
              + 0.15·seniority_fit + 0.15·semantic_similarity + 0.10·freshness)
```

Every weight and threshold lives in `config/config.yaml`; none is hardcoded.

## Consequences

- Every number is reproducible: no model decides a score.
- A withheld posting can state *why* (`matches.gate_failures`), so eligibility
  gating is visible rather than silent.
- Requirement alignment produces the evidence span as a by-product, which is
  what makes the explanation citable (§10.1).
- The weights are an assertion until Phase 2 measures them; the ablation study
  (§9.3) is what turns them from opinion into evidence.
