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

## Generated text (Phase 4)

Cover letters and bullet rewrites are produced from a structured fact bundle and
then checked token by token against the CV by the anti-invention diff (§10.3).

| Measure | Result |
|---|---|
| Adversarial cases blocked | 50 / 50 |
| Honest statements passed | 15 / 15 |
| Attempts before refusal | 1 generation, 1 repair |

The adversarial set lives in `backend/tests/unit/test_anti_invention.py` and is
part of the suite, so the number above is re-measured on every run rather than
recorded once. The honest set is measured alongside it deliberately: a guard
that blocks everything is not a guard, it is an off switch.

Both figures are for the mechanism, not for a model. The diff does not depend on
which provider produced the text, and text that fails it is never shown — so the
failure mode is a refusal, not a plausible fabrication.

## Bilingual behaviour (Phase 5)

Arabic and English are handled in one pipeline rather than two. Text is folded
identically wherever it is compared — dedup, taxonomy resolution, the
anti-invention diff — so a skill that matches in one component matches in all of
them.

| Behaviour | Status |
|---|---|
| Arabic CV section, seniority and experience extraction | Built, unit-tested |
| Mixed-script clitic segmentation (`وKafka` → `Kafka`) | Built, unit-tested |
| Arabic UI locale, stored per account, RTL | Built, verified end to end |
| NDCG@10 on the Arabic split | **Not measurable** — no Arabic postings in the corpus |

The last row is the important one. The Phase 5 exit criterion is a comparison
between two numbers and only one of them exists, so the criterion is neither met
nor missed. Nothing in this system reports a bilingual ranking quality figure,
because there is no evidence for one.

**Known limitation.** CV character-yield thresholds are calibrated on English.
Arabic expresses the same content in fewer characters, so an ordinary Arabic CV
can draw a "thinner than typical" warning it does not deserve. No correction has
been applied, because a guessed constant would be indistinguishable from a
measured one later.

## Data

Job postings from employer ATS public APIs (see
[DATA_SOURCES.md](DATA_SOURCES.md)). CVs are supplied by the user, encrypted at
rest, redacted before any hosted model call, and deleted on request.
