# Handoff — 9 September 2026

Phases 0 through 4 are built, verified and pushed to
`https://github.com/AHMED-METAWEA/CareerPilot.ai`. Everything below is the state
you are resuming from.

## Verified green

```
backend:  ruff · ruff format · mypy strict · import-linter (2 contracts) · alembic check
          523 tests passing (298 unit, 225 integration + eval)
frontend: tsc --noEmit · next lint · next build (23 routes)
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

## Two things are deliberately still red

**Negative controls (§9.4).** Cross-domain separation is 0.077 against a 0.15
threshold; shuffled pairings 0.041 against 0.10. Diagnosed, not tuned: there is
no semantic model in this configuration (the deterministic lexical fallback is
running) and the stub profile carries three skills. §9.4 says a failing control
is not a tuning opportunity, so the thresholds stand. `docs/EVALUATION.md` has
the decomposition and what would settle it.

**The golden set (§9.1).** The tooling is built; the labels are human work and
have not been done. Every ranking number in the docs is therefore provisional.

## To pick Phase 5 up

Phase 5 is the bilingual pipeline: Arabic CV parsing, mixed-script
normalisation, a dedicated Arabic evaluation split, and an Arabic UI locale.
Its exit criterion is NDCG@10 on the Arabic split within 5 points of the English
split — which cannot be measured until the golden set has labels, so expect to
build the split and the harness before the number means anything.

The normalisation half already exists (`normalize_arabic`, used by the diff and
the taxonomy), and the anti-invention diff has an Arabic test. What is missing
is the parsing, the split, and the locale.

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
