# Model card — CareerPilot matching

**Status:** skeleton. Populated in Phase 2, when there is something measured to
put in it. Publishing an unmeasured model card would be worse than publishing
none.

## What the system will produce

A ranked shortlist of live job postings for a candidate profile, with a
requirement-level assessment of fit and gap, and a decomposed score.

## What the score is not

- **Not a probability of being hired**, interviewed, or passing a screen. No
  component of the score is trained on or calibrated against hiring outcomes,
  and none could be (ADR 0004).
- **Not an "ATS compatibility" measure.** Keyword density is folklore; real ATS
  failures are parsing failures and knockout questions.
- **Not a judgement of the candidate.** It measures the distance between one
  document and another.

## Intended use

Helping a candidate decide which of many openings deserve the two hours a
serious application costs. Not for employer-side screening, ranking of
applicants, or any automated decision about a person.

## Components (to be documented with measurements in Phase 2)

| Component | Model | Measured on | Result |
|---|---|---|---|
| Embeddings | `intfloat/multilingual-e5-small` | golden set recall@50 | pending |
| Reranking | `BAAI/bge-reranker-base` | NDCG@10 ablation | pending |
| Field extraction | `llama-3.1-8b-instant` (Groq) | 100 hand-verified extractions | pending |
| Requirement analysis | `llama-3.3-70b` (Groq) | agreement with human labels | pending |

Model identifiers live in `config/config.yaml`; vendors deprecate names
frequently, so none appears in source code (§7.1).

## Known limitations already measured

**Deduplication — truncated copies.** SimHash over 5-gram shingles separates
lightly-edited syndication (0–2 bits) from unrelated postings (10–38 bits) on
real descriptions, but an aggregator publishing 85% of a description lands 5–11
bits away, inside the unrelated range. Those copies are caught by the exact-key,
canonical-URL and title-blocking stages instead; the threshold is not widened,
because widening it would merge distinct jobs. Measurements:
`backend/tests/integration/test_dedup_calibration.py`.

**Company entity resolution — subsidiaries.** "Vodafone Egypt" and "Vodafone"
are not automatically merged. A strict token subset is the shape of a regional
subsidiary, and a false merge corrupts two employers' data in a way that is hard
to detect afterwards. These go to a human review queue (§11.3, R5).

**Seniority inference.** Read from the title, falling back to the first 600
characters of the description. Where neither states a level, the field is null
rather than guessed — an invented "mid" would silently shift every score.

## Groundedness controls (Phase 1)

Extraction carries character spans that are programmatically verified against
the source document; fields failing verification are discarded rather than
surfaced. Skills must resolve to a closed taxonomy. `insufficient_evidence` is
a valid output for every extraction task, and abstention rate is monitored.

## Data

Job postings from employer ATS public APIs (see
[DATA_SOURCES.md](DATA_SOURCES.md)). CVs are supplied by the user, encrypted at
rest, redacted before any hosted model call, and deleted on request.
