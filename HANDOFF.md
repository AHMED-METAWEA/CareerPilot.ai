# Handoff — 12 September 2026

Phases 0 through 6 are built, verified and pushed to
`https://github.com/AHMED-METAWEA/CareerPilot.ai`. Everything below is the state
you are resuming from.

## Verified green

```
backend:  ruff · ruff format · mypy strict · import-linter (2 contracts) · alembic check
          627 tests passing
frontend: tsc --noEmit · next lint · next build (25 routes)
```

## What each phase left behind

- **Phase 0 — data spine.** 141 validated boards → 15,594 postings (15,067
  unique), 100% with an ATS-native apply URL. Five-stage dedup, monthly
  partitioned `raw_payloads`, Postgres task queue on `SKIP LOCKED`.
- **Phase 1 — matching core.** Grounded extraction with verified spans, closed
  taxonomy, RRF fusion over BM25 + pgvector, decomposed scoring behind boolean
  gates, percentile presentation.
- **Phase 2 — evaluation.** Metrics, ablations, negative controls, regression
  gate. Two of the three negative controls currently fail; see below.
- **Phase 3 — the product.** Auth (Argon2id, rotating single-use refresh
  tokens), applications, export and erasure, the digest, and the Next.js client.
- **Phase 4 — writing that cannot invent.** The anti-invention diff blocks 50/50
  adversarial cases and passes 15/15 honest ones. Cover letters, bullet rewrites,
  gap analysis with learning steps, interview preparation from the posting's own
  requirements. The `/matches/[id]/prepare` page is where a candidate meets all
  four.
- **Phase 5 — the bilingual pipeline.** `app/domain/text/arabic.py` owns the
  fold: bidi strip, Arabic-Indic digits, orthographic variants, and script
  segmentation. That last one fixed a real bug — Arabic attaches its conjunction
  to the following word, so a CV reading "Python وKafka" made the anti-invention
  diff refuse a truthful letter mentioning Kafka. Arabic section headings,
  seniority, remote and employment vocabulary all land in the same closed sets
  the English path uses.
- **Phase 6 — personalisation.** A logistic model over the five sub-scores,
  fitted per user with rank as a discarded control for position bias. All three
  §11.8 constraints are mechanical: 200+ deliberate events (views excluded —
  training on them closes a loop around the system's own output), weights bounded
  to ±40% and projected back onto the simplex, and a fit applied *only* after it
  beats the defaults on that user's own labelled matches. Rejected fits are
  stored with their reason. `GET /api/v1/me/personalisation` shows the candidate
  the coefficients and both NDCG figures; `DELETE` turns it off.

## Two things are deliberately still red

**Negative controls (§9.4).** Cross-domain separation is 0.077 against a 0.15
threshold; shuffled pairings 0.041 against 0.10. Diagnosed, not tuned: there is
no semantic model in this configuration (the deterministic lexical fallback is
running) and the stub profile carries three skills. §9.4 says a failing control
is not a tuning opportunity, so the thresholds stand. `docs/EVALUATION.md` has
the decomposition and what would settle it.

**The golden set (§9.1).** The tooling is built; the labels are human work and
have not been done. Every ranking number in the docs is therefore provisional.

**The Arabic split has nothing to measure.** `careerpilot eval languages` reports
zero Arabic postings in a corpus of 15,594. The Phase 5 exit criterion is
neither met nor missed, and the fix is board curation rather than code — see the
last paragraph of this file.

## To pick Phase 6 up

Phase 6 is personalisation: learning from a candidate's own saves and dismissals
(§13), within the constraint of ADR 0004 — no outcome-based learning-to-rank,
because the labels are sparse, censored, delayed and confounded. The engagement
events it needs are already being recorded (`user_job_events`, with
`clock_timestamp()` ordering).

Before that, two things would pay for themselves:

1. **MENA board curation.** Everything bilingual is built and none of it can be
   measured. Collecting real Egyptian and Gulf careers-page tokens is the single
   highest-value non-code task in the project.
2. **Golden-set labelling.** Five CVs × 60 postings against the rubric in
   `careerpilot eval rubric`. Until it exists, no ranking number in these docs
   means anything.

What Phase 5 left translated: the chrome, the shortlist, the match detail page,
the preparation page and settings. Onboarding, register/login and the
applications list are still English-only; a missing key falls back to English
rather than to a blank, so nothing breaks — it just is not translated yet.

## Environment

```bash
make db-up                                  # pgvector/pgvector:pg16 on :5433
cd backend && .venv/bin/python -m app.cli stats
cd frontend && npm run dev                  # expects the API on :8000
```

`GROQ_API_KEY` is still unset in `backend/.env`. Everything degrades honestly
without it: extraction says so, cover letters and rewrites return 503 with
wording a candidate can act on, and gaps, interview prep, matching and the whole
web client work regardless. Local model weights (`[ml]` extra, ~2.5 GB) are
still deferred behind the `EmbeddingBackend` port, which is what the failing
negative controls are measuring the absence of.

The honest gap remains board curation, not code: 137 of 141 boards are
global/EU, 1 Egyptian, 3 wider MENA. Guessed tokens do not find MENA employers.
