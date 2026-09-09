# ADR 0002 — ATS-first sourcing over aggregator-first

**Status:** accepted · **Date:** 2026-09-08

## Context

Job data can be obtained from aggregators (broad, stale, redundant, legally
fraught) or directly from the applicant-tracking systems employers publish
through (narrow, fresh, authoritative).

## Decision

Consume employer ATS public job-board APIs as the primary source. Aggregators
are a supplement, never the foundation.

## Consequences

- **The ATS platform is known by construction.** A posting fetched from
  Greenhouse's API is on Greenhouse; no detection heuristic is needed, and the
  confidence is 1.00 rather than a guess (Appendix C).
- **The apply URL is supplied, not inferred.** `absolute_url`, `hostedUrl`,
  `jobUrl` are authoritative and stored verbatim.
- **Freshness is minutes, not days** — the feed is what the employer's own
  careers page renders.
- **Coverage is bounded by curation.** Reach comes from the board registry, so
  growth is a data task (curate more boards) rather than an engineering one.
  This is the honest trade: precision over coverage (§1.3).
- Each adapter owns its container shape and field vocabulary. There is no
  shared response accessor — Lever's bare array is why (Appendix B).
