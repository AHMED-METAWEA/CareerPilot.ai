# Evaluation

**Status:** the harness, metrics, ablation runner, negative controls and CI gate
are built. The golden set is not: 300 human-graded pairs is human work, and a
benchmark a machine wrote to grade itself measures nothing.

```bash
careerpilot eval rubric                              # the grading criteria
careerpilot eval sample --profile <id>               # stratified sample to grade
careerpilot eval load-labels graded.csv --labeller amir
careerpilot eval agreement                           # the κ ≥ 0.60 gate
careerpilot eval run --check-regression              # the ablation table
careerpilot eval controls                            # needs no labels
```

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

## Negative controls — built, and currently failing

These need no labels, so they run today. Two of the three fail in the
development configuration, and that is the point of having them: they say the
score is not yet measuring fit well enough to trust a ranking metric derived
from it.

| Control | Threshold | Measured | Verdict |
|---|---|---|---|
| Cross-domain separation | ≥ 0.15 | 0.077 (in-domain 0.342, out-of-domain 0.264) | **fail** |
| Seniority monotonicity | single peak, no reversals | peak at the candidate's own level | pass |
| Shuffled pairings | ≥ 0.10 | 0.041 (true 0.356, shuffled 0.315) | **fail** |

**What the failures mean.** Both failing controls depend on semantic
similarity, and this configuration has no semantic model: without the `[ml]`
extra, the embedding backend is a hashed lexical projection that cannot relate
"NLP" to "معالجة اللغة الطبيعية" or a data-engineering CV to a data-engineering
posting except through shared words. A decomposition run confirms it — in-domain
`skill_coverage` averaged 0.012, because the test profile holds three skills
against postings listing fifteen.

**What it does not mean.** The thresholds are not the problem, and lowering them
would convert a real finding into a passing build. §9.4 is explicit: a control
that fails is not a tuning opportunity.

**What would settle it:** installing the `[ml]` extra so the real embedding and
reranker models run, and extracting a profile with a real inference key rather
than a three-skill stub. Both are single configuration changes; neither is
code.

## Negative controls — what each one is for

- A backend-engineering CV scored against nursing and legal postings. Weak
  separation means the scorer measures writing style, not fit.
- Identical postings differing only in stated seniority; the score must move
  monotonically.
- Shuffled CV–posting pairings; the score distribution must be visibly distinct
  from true pairings. Note that this control needs at least two distinct
  candidates to mean anything — with one profile in the database it reports a
  separation of exactly zero, which reads as a scorer failure and is not one.
  Synthetic candidates from other disciplines are used to keep it meaningful.

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

### Scoring — alternatives counted as separate requirements

Found by the cross-domain control: in-domain skill coverage was averaging 0.012,
which no ranking metric would have explained.

"Experience in Python **or** Java **or** Go" was expanded into three
requirements, so a candidate who met it scored one third of it. A posting
listing three such requirements gave a fully qualified candidate 4/8.

Requirement expansion is now alternation-aware: conjunctions split ("SQL **and**
PostgreSQL" is two things to know), alternations group, and a missing
alternation reports as `Python (or Java, Go)` so the gap names the options.

### Cross-lingual splits — built, and not yet measurable

Phase 5's exit criterion is NDCG@10 on the Arabic split within 5 points of the
English split. The harness now computes a four-cell language matrix — en→en,
en→ar, ar→en, ar→ar — from the shipping configuration, and `careerpilot eval
languages` reports the make-up of the corpus and the golden set without running
a match.

Running it today gives a flat answer:

```
Corpus
     en: 15594
  detected Arabic (including unset rows): 0

Labelled pairs by cell
  none — the golden set is human work (§9.1)
```

**There are no Arabic postings in the corpus, and the reason is not the one we
assumed.**

The first diagnosis was a sourcing gap: 137 of 141 boards were global or EU, so
of course nothing Arabic turned up. That diagnosis was acted on (12 September
2026). MENA-region boards went from 4 to 29 and MENA-located postings from a
handful to 407 — Bosta and Decima International in Cairo, Yassir across the
Maghreb, HALA and Lucidya and Qiddiya in Saudi Arabia, Bayut | dubizzle and Lean
Technologies in the UAE, Bank of Jordan in Amman.

Then the 391 MENA-located postings were measured for script:

```
MENA-located postings examined : 391
  containing any Arabic script : 0
  genuinely mixed-script       : 0
  primarily Arabic             : 0
```

Not a small number. Zero. Every MENA employer reachable through an international
ATS advertises in English, including employers whose product is Arabic-language
software — Lucidya sells Arabic social analytics and posts its engineering roles
in English.

This changes what the Phase 5 exit criterion can mean, so it is worth stating
plainly rather than quietly redefining:

* **ar→ar is probably not reachable from ATS-sourced postings at all.** The
  Arabic-language job market advertises on Wuzzuf, Bayt, Forasna and Tanqeeb,
  none of which publish a public API (§5.5). No amount of board curation fixes
  that; it is a partnership problem.
* **ar→en is the cell that carries the product's actual claim**, and it now has
  data. A candidate with an Arabic CV applying to English-language postings in
  Cairo or Riyadh is not an edge case in this market — it is the common case,
  and it is precisely what §1's "shared semantic space" promises to handle.

The split report is unchanged by this: it still reports `None` for any cell
below 20 labelled pairs, and every cell is still unlabelled. The exit criterion
remains **neither met nor missed**. What has changed is that the obstacle is now
correctly identified — it was never only about where the boards were.

Two decisions were made deliberately here, both of which would have been easier
to fudge:

* A split with fewer than 20 labelled pairs reports "not enough to measure"
  rather than an NDCG. A metric over a handful of pairs is noise wearing the
  costume of a measurement, and it would have let the criterion "pass".
* The cross-lingual cells (ar→en, en→ar) are reported separately from ar→ar. A
  system can score well on ar→ar by being a competent Arabic keyword matcher
  while failing completely at the shared-semantic-space claim in §1.5. Pooling
  them would hide exactly the failure the phase exists to detect.

### Known limitation — character yield is calibrated on English

`CHARS_PER_PAGE_GOOD` (1500) and `CHARS_PER_PAGE_POOR` (400) were set from
English CVs. Arabic writes the same content in fewer characters — short words,
unwritten short vowels — so a normal Arabic CV sits lower on this scale than an
equivalent English one and can draw a "thinner than a typical CV" warning it
does not deserve.

No correction factor has been applied, because there is no measurement to base
one on and a guessed constant would be indistinguishable from a real
calibration six months from now. What is needed is the character yield of
twenty or so real Arabic CVs. Until then the thresholds stand and this
paragraph is the disclosure.

### Still to measure

- Dedup precision and recall on 200 hand-labelled pairs (Phase 2)
- Company-resolution accuracy against a labelled alias set
- Whether the 45-day freshness gate matches observed posting lifetimes
