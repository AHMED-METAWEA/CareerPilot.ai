# Evaluation

**Status:** Phase 2 deliverable (§9). This file is the plan of record and the
home of measurements already taken; ranking metrics arrive with the harness.

Evaluation is a subsystem, not a phase-end activity. No feature after Phase 2
ships without a measurement, and `.github/workflows/eval.yml` fails a build on a
drop of more than 2 points in NDCG@10.

## Golden set (planned)

- 5 real CVs: graduate, mid-level, career-switcher personas
- 60 real postings per CV, sampled stratified across score deciles — labelling
  only obvious matches measures nothing
- 300 pairs graded 0–3 against a written rubric
- 3 independent annotators; agreement reported as Cohen's κ
- **Gate:** κ < 0.60 means the rubric is defective and is revised before any
  metric derived from it is trusted

## Targets

| Metric | Target |
|---|---|
| NDCG@10 | ≥ 0.75 |
| MRR | ≥ 0.60 |
| Precision@5 | ≥ 0.70 |
| Recall of grade-3 in top 10 | ≥ 0.85 |
| Dedup precision / recall | ≥ 0.95 / ≥ 0.90 |
| Extraction hallucination rate | < 2% |

## Ablation study (the headline artefact)

| Configuration | NDCG@10 | p95 latency |
|---|---|---|
| Document cosine only | — | — |
| + BM25 fusion (RRF) | — | — |
| + cross-encoder rerank | — | — |
| + decomposed scorer with gates | — | — |

## Negative controls

- A backend-engineering CV scored against nursing and legal postings. Weak
  separation would mean the scorer measures writing style, not fit.
- Identical postings differing only in stated seniority; the score must move
  monotonically.
- Shuffled CV–posting pairings; the score distribution must be visibly distinct
  from true pairings.

---

## Measurements already taken (Phase 0)

### Deduplication — SimHash calibration

**Corrected 9 September 2026.** The first calibration was measured on
descriptions that still contained HTML markup — Greenhouse returns its `content`
field entity-encoded, and `html_to_text` unescaped *after* stripping tags, so
every Greenhouse description carried literal markup. Shared boilerplate tags
inflated similarity and made the numbers look better than they were. The bug is
fixed, the corpus was rebuilt by replaying stored raw payloads, and these are
the numbers on clean text.

Seven real ATS descriptions (mean 6,000 characters), Hamming distance over
64-bit SimHash of word 5-gram shingles. Reproduced by
`backend/tests/integration/test_dedup_calibration.py`.

| Comparison | Distance |
|---|---|
| Same posting, lightly edited (trailer added, paragraph dropped, whitespace normalised) | 0–8, median 2 |
| Same posting, 15% of the tail truncated | 5–11 |
| Two different postings | 10–30, median 30 |

**What this says about the configured threshold of 3.** It catches roughly
two-thirds of light edits and keeps seven bits of margin to the nearest
unrelated pair. Raising it to 8 would catch every light edit in this sample and
leave two bits of margin — not a margin at all when the target is 0.95
precision and a false merge silently corrupts two employers' data.

The threshold stays at the plan's value. Seven documents is not enough evidence
to retune a specification, and Phase 2's 200 hand-labelled dedup pairs (§9.2)
are what should settle it. What the measurement does establish is the shape of
the trade-off, and that the misses are real: light edits beyond the threshold
are caught, if at all, by the exact-key, canonical-URL and title-blocking
stages, which do not degrade with length.

**Also measured:** distance tracks the *share* of the document that changed, not
the word count. On a 60-word stub the same trailer that moves a full description
by 1 bit moves it by 7. Clustering therefore does not trust SimHash below
`MIN_SIMHASH_CHARS` (400) and falls back to exact normalised-title matching
within an already company-blocked bucket.

### Deduplication — false-merge guards

Two guards were added after the calibration above, each with a test:

1. **Location.** Two postings whose primary locations are both known and differ
   are never merged on text similarity. A city change is a handful of
   characters and a different job.
2. **Company subsets.** A strict token subset of a known employer's name is
   never auto-merged (§11.3, R5).

### False merges found by running the real pipeline

Both were found by building the corpus for real — 100 boards, ~14,000 postings —
and looking at the output, not by a test. Both are now covered by regression
tests, and both left a detector behind, because the shared property of these
failures is that the wrong output looks entirely plausible.

**1. Every employer on one ATS merged into one company.**
The Greenhouse adapter passed a job's `absolute_url` as the employer's careers
URL. The registrable domain of that URL is `greenhouse.io`, so every Greenhouse
board resolved to the same company domain, and `ON CONFLICT (domain)` folded 13
unrelated employers — Algolia, Wolt, Tide, Typeform and others — into a single
"Adyen" row holding 937 postings.

*Fix:* `company_domain()` refuses any registrable domain on a shared ATS host,
applied in the domain layer, the service layer and the adapters. A domain
collision that does survive now queues a review row instead of merging silently.
*Detector:* `suspected_false_merges` in `/admin/stats` counts company rows
carrying more than three distinct employer names.

**2. An entire job board collapsed into one group.**
`gh_jid` was in the tracking-parameter list. Employers who render their
Greenhouse board on their own domain carry the job id in exactly that parameter
(`databricks.com/careers/open-positions/job?gh_jid=…`), so stripping it gave 870
distinct postings one identical URL — and URL equality, the strongest dedup
signal, merged them all. Stripe, MongoDB, Elastic and Instacart failed the same
way, for 2,798 postings in total.

*Fix:* two changes. `gh_jid` is no longer treated as tracking, and parameter
stripping is now split — a stored apply URL loses only parameters that cannot
possibly carry identity (`utm_*`, `fbclid`, `gclid`), while the full Appendix D
list is stripped from the comparison key alone. A parameter that looks like
tracking to us may be identity to the employer, and §12.7 already said the apply
URL is never rewritten.
*Detector:* a canonical URL shared by more than `dedup.max_postings_per_url`
postings is ignored for merging, both within a batch and corpus-wide, and
`ambiguous_apply_urls` in `/admin/stats` counts them.

**What the two have in common.** Both took an identifier from a shared namespace
and used it as an identity. Both produced output that looked correct from every
angle except the one that mattered. Neither would have been caught by a unit
test written from the specification — only by running the thing and looking.

### Text extraction — a bug the corpus itself revealed

`html_to_text` unescaped HTML entities *after* stripping tags. Greenhouse
returns entity-encoded content, so markup survived into `description_text` for
every Greenhouse posting — polluting SimHash, embeddings and requirement
extraction at once, and looking like a formatting quirk rather than a defect.

Found by reading the stored text of a posting that scored well and had no
extractable requirements. Fixed by unescaping first; the whole corpus (15,594
postings) was rebuilt by replaying `raw_payloads`, with no re-fetching and no
errors — which is what persisting payloads verbatim is for (§11.2 step 3).

### Scoring — unparseable postings scored as perfect matches

A posting whose requirements could not be extracted received `skill_coverage`
and `requirement_alignment` of 1.0 — 60% of the total weight — for telling us
nothing, and outranked postings we could actually assess.

Fixed by renormalising: terms that could not be computed are dropped and the
remaining weights rescaled, so an unassessable posting competes only on the
terms that were measurable. The affected match reports say so explicitly
("No skill requirements could be extracted from this posting") rather than
showing "0/0 requirements matched", which reads as a failed match.

### Still to measure

- Dedup precision and recall on 200 hand-labelled pairs (Phase 2)
- Company-resolution accuracy against a labelled alias set
- Whether the 45-day freshness gate matches observed posting lifetimes
