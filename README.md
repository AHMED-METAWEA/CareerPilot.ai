# CareerPilot.ai

**AI-assisted job discovery and candidate–role matching, built on verifiable data.**

CareerPilot ingests a candidate's CV, derives a structured evidence-linked
profile, continuously polls employer applicant-tracking systems for live
openings, deduplicates and verifies them, and produces a ranked shortlist in
which every position carries a requirement-level explanation of fit and gap.

It does not submit applications, does not harvest recruiter contacts, does not
send outbound email, and never adds a skill to a CV that the candidate does not
hold. Those are product boundaries, not roadmap items.

The full specification is [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md); section
references throughout the code point at it.

---

## Status

| Phase | Scope | State |
|---|---|---|
| **0 — Data spine** | Schema, six ATS adapters, normalisation, five-stage dedup, company resolution, discovery worker | **built** — 141 validated boards |
| **1 — Matching core** | CV ingestion, grounded profile extraction, gates, hybrid retrieval, rerank, decomposed scoring, apply-URL verification | **built** — 343 tests, 88% coverage |
| 2 — Evaluation | Golden set, NDCG/MRR/P@5, ablation study, CI regression gate | not started |
| 3 — Product | Multi-user auth, Next.js UI, digest, export/delete (URL verification landed early — Phase 1's gates need it) | not started |
| 4–6 | Candidate tooling · bilingual pipeline · personalisation | not started |

### Phase 0 against its exit criteria (§18)

Measured on a real corpus built from the committed board registry:

| Criterion | Target | Measured |
|---|---|---|
| Unique live postings | ≥ 5,000 | **15,067** (15,594 postings, 332 duplicate groups) |
| Postings with a populated `apply_url` | 100% | **100%** — all from the ATS-native field |
| Per-source health view | operational | `careerpilot sources list`, `GET /admin/sources` |
| Lever bare-array handled | regression test | `test_lever_returns_a_bare_array` |

`careerpilot stats` re-checks these at any time, and adds two guards that came
out of real failures: `suspected_false_merges` and `ambiguous_apply_urls`
(see [EVALUATION.md](docs/EVALUATION.md)).

### Phase 1 — what a match now consists of

```
CV → text extraction → parse-quality gate → PII redaction → schema-constrained
     extraction → span verification → taxonomy resolution → profile

profile → eligibility gates → BM25 ∪ vector → RRF → cross-encoder → top 25
        → requirement extraction → per-requirement alignment → decomposed score
```

Every claim in a match is traceable: a requirement cites the CV bullet that
answered it, a skill cites the characters in the CV that evidenced it, and the
score decomposes into the five named terms of §8.2. Scores are presented as a
percentile within the candidate's own pool and never as a probability of any
outcome.

Extraction is grounded by construction: the model returns a quote with every
field, the code verifies the quote exists in the document, and fields that fail
are discarded rather than surfaced. Skills must resolve to the curated
vocabulary in `backend/config/skills.yaml`; unresolvable tokens go to a review
queue instead of becoming skills.

**Running without model weights.** The embedding and reranker backends sit
behind protocols, with a deterministic lexical implementation used when the
`[ml]` extra is not installed. The whole pipeline runs, and `job_embeddings.model`
records which backend produced every vector, so development vectors can never be
mistaken for real ones. Install the extra to swap in
`multilingual-e5-small` and `bge-reranker-base`; nothing else changes.

**Running without an inference key.** Requirement extraction falls back to a
deterministic path (bullet segmentation plus must-have language), which is also
the baseline the §9.3 ablation measures the model against. Set `GROQ_API_KEY`
to use the model path.

## Architecture

A modular monolith plus a worker (ADR [0001](docs/adr/0001-modular-monolith.md)).
Domain boundaries are enforced by `import-linter` in CI, not by convention:

```
app/domain      pure logic, zero I/O, no framework imports
app/adapters    job sources, HTTP conduct — may import domain only
app/services    orchestration and transaction boundaries
app/api         HTTP surface        app/workers   scheduler + queue consumers
```

The pipeline narrows aggressively before any model is invoked, which is what
makes a zero-cost operating model viable (§4.2):

```
discovery → normalise + dedup → eligibility gates → hybrid retrieval
          → cross-encoder rerank → LLM analysis on the last 25
```

Everything above the LLM stage is deterministic, cacheable and free.

## Running it

Requirements: Python 3.12, Docker (for PostgreSQL 16 + pgvector).

```bash
make setup          # virtualenv and dependencies
make db-up          # PostgreSQL + pgvector on :5433
cp .env.example backend/.env   # defaults match `make db-up`
make migrate        # apply the schema
make sync           # load config/boards/*.yaml into job_sources
make run-source name=greenhouse:vercel   # one source, in the foreground
make stats          # corpus against the Phase 0 exit criteria
```

Then either run the worker (hourly discovery, dedup, maintenance):

```bash
make worker
```

or the API:

```bash
make api            # http://localhost:8000/health · /docs
```

### Curating boards

Every source is a database row, and the reviewable truth is
`backend/config/boards/*.yaml` (§5.7). A board token is a claim about the world,
so the tooling checks it:

```bash
careerpilot boards probe greenhouse vercel     # inspect one candidate
careerpilot boards validate                    # probe the whole registry
```

No token in this repository was added without a live response and a non-zero
posting count behind it: 141 boards survived out of 550 candidates probed, and
`.github/workflows/boards.yml` re-probes them weekly.

Regional coverage is honest about its gap. 137 of the 141 boards are
global-remote or EU; one is Egyptian and three are wider MENA. That is the
structural problem §5.5 describes — Wuzzuf, Bayt, Forasna and Tanqeeb publish no
public API — and closing it is board curation and partnership work, not
engineering.

## Development

```bash
make check          # ruff · mypy strict · import contracts · tests
make test-unit      # domain tests only; no database needed
```

Tests never call a third party: adapter tests replay payloads recorded from
live endpoints into `backend/tests/fixtures/`. Tests marked `db` need PostgreSQL
with pgvector and skip without it.

## What is deliberately not here

| Excluded | Why |
|---|---|
| LinkedIn / Indeed / Glassdoor data | Terms of service (§5.4) |
| Automated application submission | Violates most ATS terms; produces low-quality applications |
| Recruiter contact discovery and outreach | Legal exposure, low conversion, reframes the product as a spam tool |
| Keyword-density "ATS score" | Folklore; real ATS failures are parsing failures and knockout questions |
| Match score as hire probability | Statistically indefensible (ADR [0006](docs/adr/0006-percentile-presentation.md)) |
| Outcome-based learning-to-rank | Labels sparse, censored, delayed, confounded (ADR [0004](docs/adr/0004-no-outcome-learning.md)) |

## Documentation

- [Project plan](docs/PROJECT_PLAN.md) — the full specification
- [Data sources](docs/DATA_SOURCES.md) — per-source terms, conduct and review status
- [Evaluation](docs/EVALUATION.md) — rubric, golden set, ablations (Phase 2)
- [Model card](docs/MODEL_CARD.md) — scoring limitations, stated plainly
- [ADRs](docs/adr/) — the decisions that shaped the build

## Licence

Not yet chosen. All rights reserved until one is.
