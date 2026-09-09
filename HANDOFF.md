# Handoff — 8 September 2026

Phase 0 (data spine) is built and verified. Everything below is the state you
are resuming from.

## Verified green

```
ruff · ruff format · mypy strict · import-linter (2 contracts) · alembic check
218 tests passing (116 unit, 102 integration) · 88% coverage
```

Docker images for the API and worker both build and were smoke-tested against
the live database.

## Corpus as last built

141 validated boards → 15,594 postings, 100% with an ATS-native apply URL,
0 suspected false merges, 0 ambiguous apply URLs.

## The one thing left mid-flight

Dedup rules were tightened *after* the corpus was clustered, so the groups in
the local database are stale (harmless — grouping is derived data). To refresh:

```bash
make db-up
cd backend && .venv/bin/python -m app.cli redo-dedup --yes
.venv/bin/python -m app.cli work-once --limit 200   # ~2 min
.venv/bin/python -m app.cli stats
```

Expect group sizes in the low single digits. Before the three fixes below, one
group held 870 postings.

## What changed late, and why it matters

Three false-merge classes were found by building the corpus for real and
looking at the output — none by a test written from the spec. All three now
have regression tests and a detector; details in `docs/EVALUATION.md`.

1. **ATS host used as a company domain** — 13 employers merged into one row.
2. **`gh_jid` treated as a tracking parameter** — 870 Databricks postings
   collapsed to one URL, then merged.
3. **Location guard was not transitive** — one posting without a location
   bridged 28 cities through union-find. Fixed at cluster level, plus:
   text similarity now never merges two postings from the *same* source, since
   templated job ads make distinct requisitions look identical.

## Next step: Phase 1 (matching core)

Not started. It needs two decisions from you before the first commit:

- **Local model weights.** `intfloat/multilingual-e5-small` and
  `BAAI/bge-reranker-base` pull ~2.5 GB (torch + sentence-transformers) onto
  this machine. The `[ml]` extra exists but is not installed.
- **An inference key.** `GROQ_API_KEY` is empty. Extraction and requirement
  analysis cannot run without it (or a Gemini key, or a local Ollama).

The honest gap in Phase 0 is board curation, not code: 137 of 141 boards are
global/EU, 1 is Egyptian, 3 are wider MENA. Guessed tokens do not find MENA
employers — those need collecting from real careers pages.

Nothing is committed. `git init` was run; the working tree is untracked.
