# CareerPilot.ai — Technical Project Plan & Operating Workflow

> **AI-assisted job discovery and candidate–role matching, built on verifiable data.**

| Field | Value |
|---|---|
| **Product** | CareerPilot.ai |
| **Document** | Technical Project Plan & Operating Workflow |
| **Version** | 1.0 |
| **Status** | Approved for build |
| **Owner** | Ahmed Metawea — AI & Data Science Engineer |
| **Last updated** | 8 September 2026 |
| **Audience** | Engineering, technical reviewers, prospective clients/employers |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Definition](#2-product-definition)
3. [Scope Control](#3-scope-control)
4. [System Architecture](#4-system-architecture)
5. [Data Acquisition Strategy](#5-data-acquisition-strategy)
6. [Data Model](#6-data-model)
7. [AI/ML Architecture](#7-aiml-architecture)
8. [Matching & Scoring Specification](#8-matching--scoring-specification)
9. [Evaluation Framework](#9-evaluation-framework)
10. [Groundedness & Safety Controls](#10-groundedness--safety-controls)
11. [Operating Workflows](#11-operating-workflows)
12. [API Surface](#12-api-surface)
13. [Background Jobs](#13-background-jobs)
14. [Repository Structure](#14-repository-structure)
15. [Infrastructure & Deployment](#15-infrastructure--deployment)
16. [Security, Privacy & Compliance](#16-security-privacy--compliance)
17. [Observability & Service Levels](#17-observability--service-levels)
18. [Delivery Plan](#18-delivery-plan)
19. [Risk Register](#19-risk-register)
20. [Success Metrics](#20-success-metrics)
21. [Appendices](#21-appendices)

---

## 1. Executive Summary

### 1.1 The problem

Job seekers face a discovery problem disguised as an application problem. Openings are scattered across thousands of employer career pages, most aggregators serve stale or duplicated listings, and the candidate has no reliable way to know which of 2,000 visible roles are actually worth the two hours a serious application costs.

Existing AI job tools respond to this by automating the wrong end of the funnel: mass auto-apply, keyword-stuffed résumés, and generated cold emails. These optimise for volume, degrade the candidate's reputation, and rest on data of unknown provenance.

### 1.2 The product

CareerPilot.ai inverts the priority. It invests engineering effort in **data integrity** and **explainable matching**, and leaves the application itself to the human.

The system ingests a candidate's CV, derives a structured, evidence-linked profile, continuously polls employer applicant-tracking systems for live openings, deduplicates and verifies them, and produces a ranked shortlist in which every position carries a requirement-level explanation of fit and gap.

### 1.3 Design thesis

Three convictions shape every decision in this document.

**Data acquisition is the hard problem, not inference.** The differentiator is a job corpus that is fresh, deduplicated, legally sourced, and whose apply links resolve. Most competing projects fail here and compensate with model complexity.

**Precision beats coverage.** Forty employers whose postings are current within the hour outperform forty thousand stale aggregator rows. Coverage is a vanity metric; a dead apply link is a product defect.

**Every claim must be traceable.** Extracted profile fields carry character offsets into the source CV. Match scores decompose into named sub-scores. Requirement assessments cite the sentence that justified them. Nothing the user sees is unattributable.

### 1.4 Deliberate exclusions

CareerPilot.ai does not submit applications, does not harvest or infer recruiter contact details, does not send outbound email on the user's behalf, and does not add skills to a CV that the candidate does not hold. These are product boundaries, not roadmap items.

---

## 2. Product Definition

### 2.1 Positioning statement

> For early-career and mid-career technical professionals who lose hours to job-board noise, CareerPilot.ai is a career copilot that surfaces a small number of genuinely well-matched, verified live openings and explains its reasoning — unlike auto-apply tools, which optimise for application volume at the cost of quality and candidate reputation.

### 2.2 Primary personas

| Persona | Context | Primary need | Success signal |
|---|---|---|---|
| **Amir — recent graduate** | Cairo, 0–1 years, applying locally and remotely | Which roles will not reject him on years-of-experience alone | Applies to 5 well-fit roles instead of 60 scattershot |
| **Salma — mid-level engineer** | 4 years, employed, passive search | High-signal weekly digest, no noise | Opens the digest every week; saves 1–2 roles |
| **Karim — career switcher** | Moving from data analysis to ML engineering | An honest, specific gap analysis | Acts on the gap report; matches improve over three months |

### 2.3 Core value propositions

1. **Verified openings.** Every surfaced role is checked live before display. No 404s.
2. **Explained fit.** Requirement-by-requirement assessment with evidence from the candidate's own CV.
3. **Honest gaps.** The system states what is missing, and never invents credentials to close the gap.
4. **Eligibility gating.** Work authorisation, location, language, and seniority floors are enforced before ranking, not after.
5. **Bilingual by design.** Arabic and English CVs and job descriptions are handled in a shared semantic space.

### 2.4 Anti-goals

| Anti-goal | Rationale |
|---|---|
| Automated application submission | Violates most ATS terms of service; produces low-quality applications that harm the candidate |
| Recruiter contact discovery and cold outreach | High legal exposure under GDPR/PECL/CAN-SPAM; low conversion; reframes the product as a spam tool |
| Keyword-density "ATS score" | Largely unsupported folklore; real ATS failures are parsing failures and knockout questions |
| Match score presented as hire probability | Statistically indefensible and actively misleading |
| Learned ranker trained on application outcomes | Labels are sparse, censored by ghosting, and confounded by referrals and timing |

---

## 3. Scope Control

### 3.1 In scope — Version 1

- CV ingestion (PDF, DOCX), text extraction, quality assessment
- Structured candidate profile extraction with evidence spans
- Job discovery from ATS public job-board APIs and free aggregator APIs
- Normalisation to a single canonical schema across all sources
- Multi-stage deduplication with company entity resolution
- Eligibility gating (work authorisation, location, seniority, language)
- Hybrid retrieval, cross-encoder reranking, LLM requirement analysis
- Decomposed and explainable match scoring
- Apply-URL extraction, canonicalisation, and liveness verification
- Ranked shortlist UI with evidence display
- Application tracking (manual status transitions)
- Daily/weekly digest email
- CV parseability report
- Evaluation harness with a labelled benchmark
- Data export and hard deletion

### 3.2 Out of scope — Version 1

| Excluded | Status |
|---|---|
| LinkedIn / Indeed / Glassdoor data | Permanently excluded (terms of service) |
| Contact discovery, outreach generation, email sending | Permanently excluded |
| Automated application submission | Permanently excluded |
| Company intelligence module | Reduced to a stored careers-page link |
| Notifications service | Reduced to one scheduled digest |
| Personal analytics dashboard | Reduced to three counters on the profile page |
| Outcome-based learning-to-rank | Replaced by implicit-signal preference learning in Phase 6 |
| Microservices, message brokers, orchestration platforms | Premature for a single-operator system |

### 3.3 Change control

Any addition to scope must state: the user problem it solves, the metric it moves, and which existing item it displaces. Scope grows only by substitution during Phases 0–2.

---

## 4. System Architecture

### 4.1 Architectural style

A **modular monolith** with a separate worker process. One deployable API, one deployable worker, one database. Domain boundaries are enforced by Python package structure and import discipline rather than by network hops.

Rationale: the system is operated by one engineer. Microservices would add deployment, tracing, and consistency costs while removing no real coupling. The package boundaries are drawn so that any module can later be extracted into a service without redesign.

### 4.2 The funnel — the central design decision

Language-model inference is the scarcest resource in the system. Groq's free tier permits roughly 30 requests per minute and 6,000 tokens per minute at the organisation level. A full job description consumes 800–1,500 input tokens. Sending every discovered job to an LLM is arithmetically impossible and would be economically irrational even on a paid tier.

The pipeline therefore narrows aggressively using cheap deterministic stages before any model is invoked.

```
┌──────────────────────────────────────────────────────────────┐
│ STAGE 0  Discovery — source adapters, HTTP only              │
│          ~5,000 raw postings/day                             │
├──────────────────────────────────────────────────────────────┤
│ STAGE 1  Normalisation + Deduplication — pure Python         │
│          URL keys → blocking → SimHash → union-find          │
│          ~3,200 unique postings                              │
├──────────────────────────────────────────────────────────────┤
│ STAGE 2  Eligibility Gates — rules, boolean                  │
│          work auth · location · seniority floor · language   │
│          · freshness                                         │
│          ~400 eligible per candidate                         │
├──────────────────────────────────────────────────────────────┤
│ STAGE 3  Hybrid Retrieval — BM25 (tsvector) + vector         │
│          (pgvector), fused by Reciprocal Rank Fusion         │
│          ~120 candidates                                     │
├──────────────────────────────────────────────────────────────┤
│ STAGE 4  Cross-Encoder Rerank — local, CPU, ~40 ms/pair      │
│          ~25 finalists                                       │
├──────────────────────────────────────────────────────────────┤
│ STAGE 5  LLM Analysis — Groq, requirement extraction,        │
│          gap analysis, natural-language explanation          │
│          25 postings ≈ 35k tokens — inside free tier         │
└──────────────────────────────────────────────────────────────┘
```

Everything above Stage 5 is deterministic, cacheable, and free. This property is what makes a zero-cost operating model viable, and it is what will keep inference costs sublinear as the corpus grows.

### 4.3 Component map

| Layer | Technology | Responsibility |
|---|---|---|
| Web client | Next.js 15 + TypeScript + Tailwind | Upload, shortlist, evidence view, tracking |
| API | FastAPI + Pydantic v2 | HTTP surface, auth, validation, orchestration |
| Domain | Pure Python packages | Scoring, dedup, gating, normalisation — no I/O |
| Adapters | Protocol-based plugins | Job sources, LLM providers, embedding backends |
| Worker | APScheduler + Postgres queue | Discovery, embedding, verification, matching |
| Storage | PostgreSQL 16 + pgvector | Relational data, full-text index, vector index |
| Queue | Postgres `SELECT … FOR UPDATE SKIP LOCKED` | Durable job queue without additional infrastructure |
| Inference | Groq (primary) → Gemini → Ollama | Extraction, analysis, generation |
| Local models | sentence-transformers on CPU | Embeddings and reranking |

### 4.4 Dependency inversion

The domain layer defines protocols; adapters implement them. The domain never imports an adapter.

```python
# app/domain/ports.py
class JobSourceAdapter(Protocol):
    name: str
    def health(self) -> SourceHealth: ...
    def fetch(self, cursor: Cursor | None) -> Iterator[RawJob]: ...
    def normalize(self, raw: RawJob) -> NormalizedJob: ...

class LLMProvider(Protocol):
    def complete(self, prompt: Prompt, schema: type[BaseModel]) -> BaseModel: ...

class EmbeddingBackend(Protocol):
    dim: int
    model_id: str
    def encode(self, texts: list[str]) -> np.ndarray: ...
```

Adding a job source is one file plus one database row. Changing LLM provider is one configuration value. Neither touches core logic.

---

## 5. Data Acquisition Strategy

### 5.1 Sourcing principle

CareerPilot.ai consumes **employer applicant-tracking systems directly**, in preference to aggregators. ATS vendors publish keyless public JSON endpoints specifically so that third parties can render employer job boards. These feeds are documented, stable, legally intended for consumption, and authoritative.

This choice yields three structural advantages:

1. **ATS platform is known by construction** — no detection heuristics required for primary sources.
2. **The canonical apply URL is supplied by the ATS** — no URL inference required.
3. **Freshness is minutes, not days** — the feed is the same data the employer's own careers page renders.

### 5.2 Tier 1 — ATS public job-board APIs

| Platform | Endpoint | Auth | Container shape | Title field |
|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | None | `{jobs: [...]}` | `title` |
| Lever | `api.lever.co/v0/postings/{company}?mode=json` | None | bare array | `text` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{org}` | None | `{jobs: [...]}` | `title` |
| Workable | `apply.workable.com/api/v1/widget/accounts/{sub}?details=true` | None | `{jobs: [...]}` | `title` |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | None | `{content: [...]}` | `name` |
| Recruitee | `{company}.recruitee.com/api/offers/` | None | `{offers: [...]}` | `title` |

**Known integration hazards.**

- Greenhouse, Ashby, Workable and SmartRecruiters wrap results in an object; Lever returns a bare array. A naïve `data.get("jobs", [])` silently yields zero rows for Lever, and the failure is invisible without per-source health checks.
- Field vocabularies diverge across all six. Normalisation is per-adapter, never shared.
- The Workable endpoint is the one their embeddable careers widget calls rather than their documented authenticated API. It is public and functional, but must be treated as capable of changing without notice. It is monitored more aggressively than the others.
- Greenhouse exposes `absolute_url`, Lever `hostedUrl`, Ashby `jobUrl`. Each is the authoritative apply link for that platform and is stored verbatim.

### 5.3 Tier 2 — free aggregator APIs

| Source | Coverage | Free allowance | Notes |
|---|---|---|---|
| Adzuna | UK/EU/global, salary data | ~1,000 calls/month | Strong salary normalisation |
| Arbeitnow | EU + remote | Unauthenticated | Exposes a `visa_sponsorship` filter — high value for MENA-based candidates |
| Remotive | Remote tech | Unauthenticated | Clean JSON, moderate volume |
| RemoteOK | Remote tech | Unauthenticated | Overlaps heavily with Remotive; dedup essential |
| USAJOBS | US federal | Free with key | Narrow but authoritative |

### 5.4 Excluded sources

LinkedIn, Indeed, and Glassdoor are excluded. Scraping these platforms breaches their terms of service irrespective of the computer-misuse questions explored in *hiQ Labs v. LinkedIn*, where LinkedIn ultimately prevailed on a breach-of-contract theory. The Indeed Publisher API was retired in 2023; any guidance recommending it is stale.

### 5.5 The MENA coverage gap — stated honestly

Wuzzuf, Bayt, Forasna, and Tanqeeb — the dominant Egyptian and Gulf job platforms — publish no open public API. Bayt operates a partner programme requiring commercial agreement.

Version 1 therefore covers the MENA market **indirectly**: a substantial share of funded regional employers and multinational delivery centres run Workable, Recruitee, or Greenhouse, and their postings are reachable through Tier 1. Direct local-platform coverage requires a partnership and is explicitly a post-V1 commercial workstream, not an engineering task.

### 5.6 Crawling conduct

Applies to the URL verification worker and to any HTML fallback path.

- `robots.txt` is fetched, cached for 24 hours, and honoured.
- A descriptive `User-Agent` including a contact URL is sent on every request.
- Per-host concurrency of 1, with a minimum 2-second interval and exponential backoff on 429/503.
- Conditional requests (`If-Modified-Since`, `ETag`) are used wherever the origin supports them.
- No authentication walls are circumvented, and no anti-bot measures are defeated.

### 5.7 Source registry

Every source is a database row, not code:

```yaml
- adapter: greenhouse
  name: greenhouse:instabug
  config: { board_token: instabug }
  rate_limit_rpm: 20
  enabled: true
  robots_ok: true
```

Company board tokens are curated in `config/boards/*.yaml`, versioned in Git, and reviewed as data. Target for Phase 0: **150 curated boards**, weighted toward employers hiring in Egypt, MENA, EU-remote, and global-remote.

---

## 6. Data Model

### 6.1 Entity groups

```
Identity        users · consents · audit_log
Candidate       cv_documents · cv_versions · candidate_profiles · profile_skills
Taxonomy        skills · skill_aliases
Employer        companies · company_aliases
Ingestion       job_sources · source_runs · raw_payloads
Postings        job_groups · job_postings · job_requirements · job_embeddings
Matching        matches · match_evidence
Engagement      user_job_events · applications · application_events
Evaluation      eval_labels · model_runs
```

### 6.2 Schema (abridged DDL)

```sql
-- ── Identity ────────────────────────────────────────────────
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext UNIQUE NOT NULL,
  password_hash text NOT NULL,
  locale        text NOT NULL DEFAULT 'en',
  created_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz
);

CREATE TABLE consents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose        text NOT NULL,          -- 'processing' | 'digest' | 'analytics'
  policy_version text NOT NULL,
  granted_at     timestamptz NOT NULL,
  revoked_at     timestamptz
);

-- ── Candidate ───────────────────────────────────────────────
CREATE TABLE cv_documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  storage_key text NOT NULL,
  mime        text NOT NULL,
  sha256      char(64) NOT NULL,
  uploaded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, sha256)
);

CREATE TABLE cv_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cv_document_id  uuid NOT NULL REFERENCES cv_documents(id) ON DELETE CASCADE,
  version         int  NOT NULL,
  raw_text        text NOT NULL,
  parse_quality   numeric(3,2) NOT NULL,   -- 0.00–1.00
  parser_version  text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE candidate_profiles (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  cv_version_id         uuid NOT NULL REFERENCES cv_versions(id),
  years_experience      numeric(4,1),
  seniority_level       text,               -- intern|junior|mid|senior|staff|principal
  locations             text[],
  work_auth             jsonb,              -- {country: status}
  languages             jsonb,              -- [{lang, cefr}]
  summary               text,
  extraction_confidence numeric(3,2),
  extracted_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE profile_skills (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    uuid NOT NULL REFERENCES candidate_profiles(id) ON DELETE CASCADE,
  skill_id      uuid NOT NULL REFERENCES skills(id),
  years         numeric(4,1),
  proficiency   text,
  evidence_span int4range NOT NULL,        -- offsets into cv_versions.raw_text
  source        text NOT NULL              -- 'extracted' | 'user_confirmed'
);

-- ── Taxonomy ────────────────────────────────────────────────
CREATE TABLE skills (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name text UNIQUE NOT NULL,
  esco_uri       text,
  o_net_code     text,
  kind           text NOT NULL             -- tool|language|framework|domain|soft
);

CREATE TABLE skill_aliases (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id uuid NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  alias    text NOT NULL,
  lang     text NOT NULL DEFAULT 'en',
  UNIQUE (alias, lang)
);

-- ── Employer ────────────────────────────────────────────────
CREATE TABLE companies (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name text NOT NULL,
  domain         text UNIQUE,
  hq_country     text,
  size_bucket    text,
  careers_url    text
);

CREATE TABLE company_aliases (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  alias      text NOT NULL,
  lang       text NOT NULL DEFAULT 'en',
  UNIQUE (alias, lang)
);

-- ── Ingestion ───────────────────────────────────────────────
CREATE TABLE job_sources (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  adapter        text NOT NULL,
  name           text UNIQUE NOT NULL,
  config         jsonb NOT NULL,
  enabled        bool NOT NULL DEFAULT true,
  robots_ok      bool NOT NULL DEFAULT true,
  rate_limit_rpm int  NOT NULL DEFAULT 20
);

CREATE TABLE source_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id   uuid NOT NULL REFERENCES job_sources(id),
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  fetched     int DEFAULT 0,
  new_count   int DEFAULT 0,
  errors      int DEFAULT 0,
  status      text NOT NULL DEFAULT 'running'
);

CREATE TABLE raw_payloads (
  id          uuid NOT NULL DEFAULT gen_random_uuid(),
  source_id   uuid NOT NULL,
  run_id      uuid NOT NULL,
  external_id text NOT NULL,
  payload     jsonb NOT NULL,
  fetched_at  timestamptz NOT NULL DEFAULT now()
) PARTITION BY RANGE (fetched_at);

-- ── Postings ────────────────────────────────────────────────
CREATE TABLE job_groups (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_posting_id uuid,
  cluster_key         text NOT NULL,
  first_seen_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE job_postings (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_group_id     uuid REFERENCES job_groups(id),
  source_id        uuid NOT NULL REFERENCES job_sources(id),
  external_id      text NOT NULL,
  company_id       uuid REFERENCES companies(id),
  title            text NOT NULL,
  title_normalized text NOT NULL,
  description_text text NOT NULL,
  locations        jsonb,
  remote_type      text,                    -- onsite|hybrid|remote
  employment_type  text,
  seniority_level  text,
  salary_min       numeric, salary_max numeric, currency char(3),
  posted_at        timestamptz,
  expires_at       timestamptz,
  ats_platform     text,
  ats_confidence   numeric(3,2),
  detection_method text,                    -- construction|url|fingerprint|redirect
  apply_url        text NOT NULL,
  source_url       text NOT NULL,
  canonical_url    text,
  url_status       text NOT NULL DEFAULT 'unknown',
  last_verified_at timestamptz,
  content_simhash  bigint,
  language         char(2),
  status           text NOT NULL DEFAULT 'open',
  UNIQUE (source_id, external_id)
);

CREATE TABLE job_requirements (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  posting_id   uuid NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
  text         text NOT NULL,
  kind         text NOT NULL,               -- skill|experience|education|auth|language
  is_must_have bool NOT NULL DEFAULT false,
  skill_id     uuid REFERENCES skills(id),
  span         int4range NOT NULL
);

CREATE TABLE job_embeddings (
  posting_id uuid PRIMARY KEY REFERENCES job_postings(id) ON DELETE CASCADE,
  model      text NOT NULL,
  dim        int  NOT NULL,
  embedding  halfvec(384) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ── Matching ────────────────────────────────────────────────
CREATE TABLE matches (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  profile_id    uuid NOT NULL REFERENCES candidate_profiles(id),
  posting_id    uuid NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
  total_score   numeric(5,4) NOT NULL,
  percentile    numeric(5,2),
  gate_passed   bool NOT NULL,
  gate_failures text[],
  subscores     jsonb NOT NULL,
  rank          int,
  model_version text NOT NULL,
  computed_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, posting_id, model_version)
);

CREATE TABLE match_evidence (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  match_id       uuid NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  requirement_id uuid NOT NULL REFERENCES job_requirements(id),
  status         text NOT NULL,            -- met|partial|missing|unknown
  cv_span        int4range,
  similarity     numeric(4,3),
  note           text
);

-- ── Engagement ──────────────────────────────────────────────
CREATE TABLE user_job_events (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  posting_id uuid NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
  event      text NOT NULL,                -- viewed|saved|dismissed|applied
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE applications (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  posting_id    uuid NOT NULL REFERENCES job_postings(id),
  cv_version_id uuid NOT NULL REFERENCES cv_versions(id),
  status        text NOT NULL DEFAULT 'applied',
  applied_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, posting_id)
);

CREATE TABLE application_events (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  from_status    text, to_status text NOT NULL,
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  note           text
);

-- ── Evaluation ──────────────────────────────────────────────
CREATE TABLE eval_labels (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL REFERENCES candidate_profiles(id),
  posting_id uuid NOT NULL REFERENCES job_postings(id),
  grade      smallint NOT NULL CHECK (grade BETWEEN 0 AND 3),
  labeler    text NOT NULL,
  labeled_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, posting_id, labeler)
);

CREATE TABLE model_runs (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  component text NOT NULL,
  model     text NOT NULL,
  version   text NOT NULL,
  params    jsonb,
  metrics   jsonb,
  run_at    timestamptz NOT NULL DEFAULT now()
);
```

### 6.3 Indexes

```sql
CREATE INDEX ON job_postings (company_id, title_normalized);
CREATE INDEX ON job_postings (posted_at DESC) WHERE status = 'open';
CREATE INDEX ON job_postings (last_verified_at) WHERE status = 'open';
CREATE INDEX job_fts_idx ON job_postings
  USING GIN (to_tsvector('simple', title || ' ' || description_text));
CREATE INDEX job_vec_idx ON job_embeddings
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX ON matches (user_id, total_score DESC) WHERE gate_passed;
```

### 6.4 Storage policy

- `raw_payloads` is partitioned monthly and pruned after 90 days.
- `description_text` is retained for processing; full descriptions are never republished.
- CV binaries are encrypted at rest; `cv_versions.raw_text` is purged 30 days after account deletion request, with the deletion recorded in `audit_log`.
- Embeddings carry their `model` identifier, enabling dual-write migration when the embedding model changes.

---

## 7. AI/ML Architecture

### 7.1 Model inventory

| Component | Model | Location | Rationale |
|---|---|---|---|
| CV/JD embeddings | `intfloat/multilingual-e5-small` (384-d) | Local, CPU | Arabic + English in one space; fast; small HNSW index |
| Reranking | `BAAI/bge-reranker-base` | Local, CPU | Cross-attention scoring; ~40 ms/pair |
| Field extraction | `llama-3.1-8b-instant` via Groq | Hosted | High request allowance; sufficient for schema-constrained extraction |
| Requirement analysis | `llama-3.3-70b` via Groq | Hosted | Reasoning quality on top-25 finalists only |
| Fallback | Gemini free tier → Ollama local | Hosted / local | Continuity when rate limits bind |

Model identifiers live in configuration. Vendors deprecate model names frequently; no identifier appears in source code.

### 7.2 Embedding strategy

Two granularities are embedded and stored:

- **Document level** — whole-posting and whole-profile vectors, used for Stage 3 retrieval.
- **Sentence level** — individual job requirements and individual CV bullets, used for requirement-level alignment in Stage 5.

Sentence-level embeddings are the source of per-requirement evidence, and they are considerably more discriminative than document similarity, which saturates in a narrow band because it is dominated by domain topicality rather than fit.

Vectors are stored as `halfvec` to halve index memory at negligible recall cost. `ef_search` is tuned empirically and validated against exact nearest-neighbour search on a held-out sample; recall@50 must exceed 0.98 before an HNSW configuration is accepted.

### 7.3 Retrieval

Reciprocal Rank Fusion over two independent retrievers:

```
RRF(d) = Σ  1 / (k + rank_r(d)),   k = 60
        r∈{bm25, vector}
```

Lexical retrieval catches exact tool and framework names that embeddings blur together (`PyTorch` vs `TensorFlow` are close in vector space and categorically different in a requirement). Vector retrieval catches paraphrase and cross-lingual equivalence. Neither alone is sufficient; the ablation study in §9 quantifies this.

### 7.4 LLM usage discipline

Language models are used for exactly three tasks:

1. **Schema-constrained extraction** — CV fields and job requirements, temperature 0, Pydantic-validated, one repair retry, then hard failure.
2. **Requirement assessment** — for top-25 finalists, classify each requirement as met/partial/missing with a citation.
3. **Natural-language phrasing** — turning a structured fact bundle into prose for explanations and cover letters.

Language models are never used for scoring, ranking, deduplication, or any decision that must be reproducible. Every number in the system comes from deterministic code.

---

## 8. Matching & Scoring Specification

### 8.1 Rejected approach

Cosine similarity between a whole-CV embedding and a whole-JD embedding is rejected. It is dominated by topical overlap, compresses genuine differences into a 0.60–0.85 band, is insensitive to hard disqualifiers, and cannot be explained to a user.

### 8.2 Scoring function

```
gate  = Π gate_i                              # boolean; any failure ⇒ score 0
score = gate × ( 0.35 · skill_coverage
               + 0.25 · requirement_alignment
               + 0.15 · seniority_fit
               + 0.15 · semantic_similarity
               + 0.10 · freshness )
```

### 8.3 Gates

Evaluated before any expensive computation. A failed gate excludes the posting and records the reason in `matches.gate_failures`, so the user can see *why* a role was withheld.

| Gate | Rule |
|---|---|
| Work authorisation | Posting requires authorisation the candidate lacks and offers no sponsorship |
| Location | Onsite/hybrid role outside the candidate's declared commutable set |
| Seniority floor | Posting states a hard minimum years figure exceeding candidate years by > 2 |
| Language | Posting requires a language at a level the candidate has not declared |
| Freshness | `posted_at` older than 45 days, or `url_status` not `live` |

### 8.4 Sub-scores

**`skill_coverage`** — must-have skills extracted from the posting, matched against `profile_skills`. Exact match through the canonical taxonomy; fuzzy match where skill-name cosine exceeds 0.82. Must-haves weighted 3× nice-to-haves. Reported to the user as an explicit `matched / total` fraction.

**`requirement_alignment`** — for each requirement sentence, the maximum cosine against any CV bullet; the mean of those maxima. This yields both the score and, as a by-product, the evidence span for every requirement.

**`seniority_fit`** — ordinal distance over `{intern, junior, mid, senior, staff, principal}`, with an asymmetric penalty: under-qualification is penalised approximately twice as heavily as over-qualification, but neither is free.

**`semantic_similarity`** — the cross-encoder score from Stage 4, min-max normalised within the candidate's pool. Not raw cosine.

**`freshness`** — exponential decay with a 7-day half-life.

### 8.5 Presentation of the score

Two rules are enforced in the UI layer and in every generated explanation:

1. The score is **never** described as a probability of being hired, receiving an interview, or passing a screen.
2. The score is presented as a **percentile within the candidate's own pool** — *"top 4% of the 1,240 roles reviewed for you this week"* — rather than as an absolute figure.

Percentile presentation is self-calibrating and resistant to the score inflation that occurs when an absolute number is shown against a shifting corpus.

---

## 9. Evaluation Framework

Evaluation is not a phase-end activity. It is a subsystem, built in Phase 2, and no subsequent feature ships without a measurement.

### 9.1 Golden set

- 5 real CVs spanning graduate, mid-level, and career-switcher personas
- 60 real postings per CV, sampled stratified across score deciles to avoid only labelling obvious matches
- 300 pairs graded 0–3 against a written rubric
- Three independent annotators; inter-annotator agreement reported as Cohen's κ
- **Gate:** if κ < 0.60, the rubric is defective and is revised before any metric is trusted

### 9.2 Offline metrics

| Metric | Purpose | Target |
|---|---|---|
| NDCG@10 | Primary ranking quality | ≥ 0.75 |
| MRR | First-relevant position | ≥ 0.60 |
| Precision@5 | Top-of-list quality | ≥ 0.70 |
| Recall of grade-3 in top 10 | Does the best role surface? | ≥ 0.85 |
| Dedup precision / recall | Entity resolution quality | ≥ 0.95 / ≥ 0.90 |
| Extraction hallucination rate | Groundedness | < 2% |

### 9.3 Ablation study

The headline artefact of the project. Each configuration is run against the identical golden set and reported with latency:

| Configuration | NDCG@10 | p95 latency |
|---|---|---|
| Document cosine only | — | — |
| + BM25 fusion (RRF) | — | — |
| + cross-encoder rerank | — | — |
| + decomposed scorer with gates | — | — |

A table demonstrating measured improvement across four configurations is worth more to a technical reviewer than ten additional features.

### 9.4 Negative controls

- A backend-engineering CV scored against nursing and legal postings. Weak separation indicates the scorer is measuring writing style rather than fit.
- Identical postings differing only in stated seniority. Score must move monotonically.
- Shuffled CV–posting pairings. Score distribution must be visibly distinct from true pairings.

### 9.5 Scaled judgement

An LLM judge, prompted with the same rubric, is calibrated against the human golden set. Its agreement coefficient with human labels is reported alongside any metric it produces. It is never used as an unqualified ground truth.

### 9.6 Regression gate

The evaluation harness runs in CI on every change to `domain/matching`, `domain/scoring`, or any model configuration. A drop of more than 2 points in NDCG@10 fails the build.

---

## 10. Groundedness & Safety Controls

### 10.1 Extraction is not generation

Every extracted field carries a character span into its source document. After extraction, the pipeline programmatically verifies that the cited substring exists at the cited offsets. Fields failing this check are discarded, not surfaced. This single mechanism eliminates the majority of fabrication without relying on prompt instructions.

### 10.2 Closed-world vocabularies

Extracted skills must resolve to the `skills` taxonomy via `skill_aliases`. An unresolvable token is recorded as `unmapped` and queued for taxonomy review. It is never silently invented into a canonical skill.

### 10.3 Anti-invention diff

For any CV rewriting or bullet-suggestion feature, generated output is tokenised and every skill, employer, credential, and date token is checked against the source CV. The presence of any new such token fails the response and triggers regeneration. This converts the "never invent skills or experience" requirement from a prompt instruction into a mechanical guarantee.

### 10.4 Facts from database, prose from model

Cover letters, explanations, and interview preparation receive a structured fact bundle assembled from the database. The prompt forbids the introduction of entities not present in that bundle, and the anti-invention diff enforces it.

### 10.5 Abstention

`insufficient_evidence` is a valid output for every extraction and assessment task. A model that declines is preferable to a model that guesses, and abstention rate is monitored as a health metric.

### 10.6 Measurement

100 extractions are hand-verified per model change. Hallucination rate is recorded in `model_runs.metrics` and tracked over time. The metric is published in the project's model card.

---

## 11. Operating Workflows

### 11.1 W1 — Onboarding and profile construction

```
1  User registers; consent recorded with policy version
2  User uploads CV (PDF/DOCX, ≤ 10 MB)
3  Virus scan → text extraction (pdfplumber / python-docx)
4  Parse-quality score computed:
      character yield · section detection · table/column detection
5  IF parse_quality < 0.6 → surface parseability report, request a
      simpler layout, halt
6  PII redaction (name, email, phone, address) before any hosted model call
7  Schema-constrained extraction → candidate_profiles + profile_skills,
      every field carrying an evidence span
8  Span verification; failed fields discarded
9  Skills resolved against taxonomy; unmapped tokens flagged
10 Profile presented for confirmation
11 User corrections written with source = 'user_confirmed'
      → these are the highest-value training signal in the system
12 Profile embedded (document + per-bullet)
```

**Exit criteria:** confirmed profile with declared work authorisation, target locations, and language levels. The system does not proceed to matching without these; they drive the gates.

### 11.2 W2 — Discovery (continuous)

```
Hourly, per enabled source, rate-limit-aware:

1  Worker claims a source via SKIP LOCKED
2  Adapter.fetch() with cursor → raw payloads
3  Payloads persisted verbatim to raw_payloads (replayable)
4  Adapter.normalize() → NormalizedJob (strict Pydantic)
5  Company resolution: domain → alias table → fuzzy match
      · no confident match ⇒ new company row queued for review
6  ATS platform assigned:
      Tier 1 source           ⇒ by construction, confidence 1.00
      aggregator              ⇒ URL host pattern, confidence 0.95
      unresolved              ⇒ HTML fingerprint, confidence 0.80
      still unresolved        ⇒ 'unknown' (a valid value)
7  Apply-URL resolution in priority order:
      ATS-native field  >  rel=canonical  >  JSON-LD JobPosting.url
      >  redirect-chain terminus  >  source_url
8  source_url, canonical_url, apply_url all stored; none overwritten
9  source_runs updated: fetched / new / errors
10 Health check: a run returning zero rows for a previously
      productive source raises an alert
```

### 11.3 W3 — Normalisation and deduplication

Five stages, ordered cheapest first. No stage compares across blocking keys.

```
1  EXACT KEY        (source_id, external_id) → same posting, update in place
2  CANONICAL URL    strip utm_*, gh_src, ref, fbclid; resolve redirects;
                    compare normalised URL
3  BLOCKING         bucket = (company_id, first 3 normalised title tokens)
4  NEAR-DUPLICATE   SimHash-64 over 5-gram shingles of description;
                    Hamming distance ≤ 3 within bucket
5  SEMANTIC         cosine > 0.94 within bucket — tie-breaker only
6  CLUSTER          union-find → job_group
7  CANONICAL PICK   member on the employer's own ATS wins;
                    aggregator copies retained as corroborating evidence
```

**The genuine difficulty is company entity resolution, not text similarity.** "Vodafone Egypt", "_VOIS", "Vodafone Intelligent Solutions", and "فودافون مصر" must resolve to one entity. `company_aliases` is treated as curated data with a human review queue, and alias coverage is a tracked metric.

### 11.4 W4 — Matching (nightly, and on demand)

```
For each active profile:

STAGE 2  Gates          → ~400 eligible from ~3,200 open
STAGE 3  Retrieval      BM25 top-200 ∪ vector top-200 → RRF → top-120
STAGE 4  Rerank         cross-encoder on 120 pairs (~5 s CPU) → top-25
STAGE 5  LLM analysis   requirement extraction + assessment on 25
         Scoring        deterministic sub-scores → total → percentile
         Persistence    matches + match_evidence, versioned by model_version
         Idempotency    (profile_id, posting_id, model_version) unique
```

Token budget per run: approximately 35,000 tokens, comfortably inside free-tier allowances. Runs are staggered across users to respect per-minute limits, and a circuit breaker halts the stage on sustained 429 responses rather than retrying into a hard cap.

### 11.5 W5 — Link verification (every 12 hours)

```
1  Select open postings ordered by last_verified_at ascending
2  Respect robots.txt and per-host rate limits
3  HEAD → GET; follow redirects
4  Assert: HTTP 200
        · company token present on page
        · title token present on page
        · closure phrases absent
5  Set url_status ∈ {live, redirected, gone, blocked, unknown}
6  Postings not verified within 48 h are suppressed from display
      until re-verified
```

A posting is never shown to a user without a `live` status inside the verification window. This is the single most visible quality guarantee in the product.

### 11.6 W6 — Candidate review and application

```
1  User opens ranked shortlist (percentile-presented)
2  Each card shows: role · company · location · freshness
                    · matched requirements (n/m) · top gaps
3  Expanded view shows requirement-by-requirement evidence,
      each citing the CV span that justified it
4  User acts: save · dismiss · apply
      → user_job_events (dense implicit signal)
5  "Apply" opens the verified apply_url in a new tab and creates an
      applications row with a snapshot of the cv_version used
6  Duplicate-application guard fires if the job_group is already applied to
7  User advances status manually; each transition writes application_events
```

The system never submits an application. The handoff is deliberate and visible.

### 11.7 W7 — Digest (daily, 07:00 local)

New matches above the user's percentile threshold, capped at five, with a one-line reason each. Delivery is a single scheduled email. Unsubscribe is honoured immediately and recorded in `consents`.

### 11.8 W8 — Preference learning (Phase 6)

Activated only once a user exceeds 200 engagement events. A small logistic model over the existing sub-scores adjusts per-user weights. Constraints: interpretable coefficients only, weights bounded to ±40% of defaults, and the ablation harness must show improvement on that user's own labelled subset before the personalised weights are applied.

Outcome-based learning is not implemented. Application outcomes are sparse, censored by non-response, delayed by weeks, and confounded by referrals, timing, and competition. A model trained on them would learn noise and present it with false confidence.

---

## 12. API Surface

Versioned under `/api/v1`. JWT access tokens with refresh rotation. All list endpoints are cursor-paginated.

### 12.1 Authentication

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Create account; records consent and policy version |
| `POST` | `/auth/login` | Issue access + refresh tokens |
| `POST` | `/auth/refresh` | Rotate refresh token |
| `POST` | `/auth/logout` | Revoke refresh token |

### 12.2 Candidate profile

| Method | Path | Description |
|---|---|---|
| `POST` | `/cv` | Upload CV; returns `cv_version_id` and `parse_quality` |
| `GET` | `/cv/{id}/parseability` | Structured parseability report |
| `GET` | `/profile` | Current profile with per-field confidence and evidence spans |
| `PATCH` | `/profile` | User corrections; writes `source = 'user_confirmed'` |
| `POST` | `/profile/skills` | Add or confirm a skill |
| `DELETE` | `/profile/skills/{id}` | Remove an extracted skill |

### 12.3 Matching

| Method | Path | Description |
|---|---|---|
| `POST` | `/matches/refresh` | Enqueue a matching run; returns `job_id` |
| `GET` | `/matches` | Ranked list — `?min_percentile&remote&location&cursor` |
| `GET` | `/matches/{id}` | Full analysis: sub-scores, requirement evidence, gaps, `apply_url`, `last_verified_at` |
| `GET` | `/matches/withheld` | Gated postings with the reason each was excluded |

### 12.4 Engagement

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs/{id}/events` | `viewed` / `saved` / `dismissed` / `applied` |
| `GET` | `/applications` | Tracked applications with status |
| `POST` | `/applications` | Record an application |
| `PATCH` | `/applications/{id}` | Advance status; writes an event row |

### 12.5 Account and compliance

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/export` | Full data export (JSON + original CV files) |
| `DELETE` | `/me` | Hard-delete request; cascades and writes to `audit_log` |
| `GET` | `/me/consents` | Consent history |
| `PATCH` | `/me/consents` | Grant or revoke by purpose |

### 12.6 Administration

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/sources` | Source registry with health metrics |
| `POST` | `/admin/sources/{id}/run` | Trigger a discovery run |
| `GET` | `/admin/companies/review` | Unresolved company alias queue |
| `GET` | `/admin/skills/unmapped` | Unmapped skill token queue |
| `GET` | `/admin/eval/latest` | Most recent evaluation harness results |

### 12.7 Conventions

- Errors follow RFC 7807 (`application/problem+json`).
- Every response carries `X-Request-ID` for trace correlation.
- Rate limits: 60 req/min per user; `/matches/refresh` limited to 3 per hour.
- `apply_url` is returned verbatim. It is never shortened, proxied, wrapped, or rewritten.

---

## 13. Background Jobs

| Job | Schedule | Concurrency | Description |
|---|---|---|---|
| `discover` | Hourly per source | 4 workers | Fetch, normalise, persist raw payloads |
| `resolve_entities` | On new postings | 2 | Company and skill resolution; queues unknowns |
| `deduplicate` | On new postings | 2 | Five-stage clustering into `job_groups` |
| `embed` | Batched, every 15 min | 1 | Document and sentence embeddings for new postings |
| `verify_urls` | Every 12 h, oldest first | 2, host-limited | Liveness verification; sets `url_status` |
| `match_users` | Nightly 02:00 + on demand | 1, staggered | Full funnel per active profile |
| `digest` | Daily 07:00 local | 1 | Compose and send digest |
| `expire_postings` | Daily | 1 | Close postings past `expires_at` or `gone` |
| `prune_raw` | Weekly | 1 | Drop `raw_payloads` partitions older than 90 days |
| `run_eval` | On CI + weekly | 1 | Evaluation harness; writes `model_runs` |

### 13.1 Queue mechanics

```sql
UPDATE task_queue
   SET status = 'running', locked_at = now(), locked_by = $1
 WHERE id = (
   SELECT id FROM task_queue
    WHERE status = 'pending' AND run_after <= now()
    ORDER BY priority DESC, run_after
    FOR UPDATE SKIP LOCKED
    LIMIT 1
 )
RETURNING *;
```

Durable, transactional, observable through ordinary SQL, and requires no additional infrastructure. Redis is introduced only when pub/sub or a genuine cache need arises.

### 13.2 Failure policy

- Exponential backoff with jitter: 1 min, 5 min, 25 min, then dead-letter.
- 429 responses honour the `Retry-After` header and consume the worker's rate budget for that source.
- A circuit breaker opens after five consecutive failures on a source, disabling it and raising an alert rather than retrying into a hard quota.
- Every task is idempotent by construction; replaying a task must not duplicate rows.

---

## 14. Repository Structure

```
careerpilot/
├── README.md
├── docs/
│   ├── PROJECT_PLAN.md              ← this document
│   ├── MODEL_CARD.md                ← scoring limitations, honest caveats
│   ├── EVALUATION.md                ← rubric, golden set, ablation results
│   ├── DATA_SOURCES.md              ← per-source terms and conduct notes
│   └── adr/                         ← architecture decision records
│       ├── 0001-modular-monolith.md
│       ├── 0002-ats-first-sourcing.md
│       ├── 0003-decomposed-scoring.md
│       └── 0004-no-outcome-learning.md
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py                ← model IDs, thresholds, weights
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   ├── errors.py
│   │   │   └── v1/
│   │   │       ├── auth.py
│   │   │       ├── profile.py
│   │   │       ├── matches.py
│   │   │       ├── applications.py
│   │   │       ├── account.py
│   │   │       └── admin.py
│   │   │
│   │   ├── domain/                  ← pure logic, zero I/O, fully unit-tested
│   │   │   ├── ports.py             ← protocols
│   │   │   ├── models.py            ← NormalizedJob, CandidateProfile, …
│   │   │   ├── profile/
│   │   │   │   ├── extraction.py
│   │   │   │   ├── spans.py         ← evidence-span verification
│   │   │   │   └── parseability.py
│   │   │   ├── jobs/
│   │   │   │   ├── normalize.py
│   │   │   │   ├── urls.py          ← canonicalisation, apply-URL priority
│   │   │   │   └── ats_detect.py
│   │   │   ├── dedup/
│   │   │   │   ├── blocking.py
│   │   │   │   ├── simhash.py
│   │   │   │   ├── clustering.py
│   │   │   │   └── entities.py      ← company resolution
│   │   │   ├── matching/
│   │   │   │   ├── gates.py
│   │   │   │   ├── retrieval.py     ← BM25 + vector + RRF
│   │   │   │   └── rerank.py
│   │   │   ├── scoring/
│   │   │   │   ├── subscores.py
│   │   │   │   ├── aggregate.py
│   │   │   │   └── percentile.py
│   │   │   └── safety/
│   │   │       ├── redaction.py
│   │   │       ├── anti_invention.py
│   │   │       └── taxonomy.py
│   │   │
│   │   ├── adapters/
│   │   │   ├── sources/
│   │   │   │   ├── base.py
│   │   │   │   ├── greenhouse.py
│   │   │   │   ├── lever.py
│   │   │   │   ├── ashby.py
│   │   │   │   ├── workable.py
│   │   │   │   ├── smartrecruiters.py
│   │   │   │   ├── recruitee.py
│   │   │   │   ├── adzuna.py
│   │   │   │   ├── arbeitnow.py
│   │   │   │   ├── remotive.py
│   │   │   │   └── usajobs.py
│   │   │   ├── llm/
│   │   │   │   ├── base.py
│   │   │   │   ├── groq.py
│   │   │   │   ├── gemini.py
│   │   │   │   └── ollama.py
│   │   │   ├── embeddings/
│   │   │   │   ├── base.py
│   │   │   │   └── local_st.py
│   │   │   └── storage/
│   │   │       └── object_store.py
│   │   │
│   │   ├── services/                ← orchestration, transaction boundaries
│   │   │   ├── ingestion.py
│   │   │   ├── profiling.py
│   │   │   ├── matching.py
│   │   │   ├── verification.py
│   │   │   └── digest.py
│   │   │
│   │   ├── workers/
│   │   │   ├── runner.py
│   │   │   ├── queue.py
│   │   │   └── tasks/
│   │   │
│   │   ├── db/
│   │   │   ├── session.py
│   │   │   ├── models.py
│   │   │   └── migrations/
│   │   │
│   │   └── eval/
│   │       ├── harness.py
│   │       ├── metrics.py           ← NDCG, MRR, P@k, κ
│   │       ├── ablation.py
│   │       ├── golden/              ← labelled benchmark, versioned
│   │       └── report.py
│   │
│   ├── config/
│   │   └── boards/
│   │       ├── egypt.yaml
│   │       ├── mena.yaml
│   │       ├── eu_remote.yaml
│   │       └── global_remote.yaml
│   │
│   └── tests/
│       ├── unit/                    ← domain, no I/O, fast
│       ├── integration/             ← adapters against recorded fixtures
│       ├── fixtures/                ← frozen ATS payloads per platform
│       └── eval/                    ← regression gate
│
├── frontend/
│   ├── package.json
│   ├── app/
│   │   ├── (auth)/
│   │   ├── onboarding/
│   │   ├── matches/
│   │   │   ├── page.tsx
│   │   │   └── [id]/page.tsx        ← evidence view
│   │   ├── applications/
│   │   └── settings/
│   ├── components/
│   │   ├── MatchCard.tsx
│   │   ├── EvidencePanel.tsx        ← requirement ↔ CV span highlighting
│   │   ├── GapReport.tsx
│   │   └── ParseabilityReport.tsx
│   └── lib/api.ts
│
├── infra/
│   ├── docker-compose.yml
│   ├── Dockerfile.api
│   ├── Dockerfile.worker
│   └── caddy/
│
└── .github/workflows/
    ├── ci.yml                       ← lint, type-check, unit, integration
    └── eval.yml                     ← evaluation regression gate
```

### 14.1 Import discipline

Enforced in CI by `import-linter`:

- `domain` may import nothing from `adapters`, `services`, `api`, or `db`.
- `adapters` may import `domain` only.
- `services` may import `domain`, `adapters`, `db`.
- `api` may import `services` and `domain`.

This is what keeps the monolith modular and makes future service extraction mechanical rather than archaeological.

---

## 15. Infrastructure & Deployment

### 15.1 Target environment

| Component | Host | Cost |
|---|---|---|
| API + worker + Postgres | Oracle Cloud Always Free — ARM Ampere, 4 vCPU / 24 GB RAM | $0 |
| Frontend | Vercel Hobby | $0 |
| Object storage (CVs) | Cloudflare R2 free tier | $0 |
| Transactional email | Resend / Brevo free tier | $0 |
| Error tracking | Sentry free tier | $0 |
| CI | GitHub Actions free minutes | $0 |

The Oracle ARM free tier is selected specifically because it is the only widely available zero-cost host with sufficient RAM to run the local embedding and reranker models alongside Postgres. Render, Fly, and Railway free tiers cannot hold the model weights, which would force those stages onto a paid API and break the cost model.

### 15.2 Deployment topology

```
              ┌─────────────┐
   Internet ──│   Caddy     │── TLS termination, HTTP/3
              └──────┬──────┘
                     │
        ┌────────────┴────────────┐
        │                         │
  ┌─────▼──────┐          ┌───────▼───────┐
  │ api        │          │ worker        │
  │ FastAPI    │          │ APScheduler   │
  │ uvicorn ×2 │          │ + task runner │
  └─────┬──────┘          └───────┬───────┘
        │                         │
        └────────────┬────────────┘
                     │
            ┌────────▼────────┐
            │ PostgreSQL 16   │
            │ + pgvector      │
            └─────────────────┘
```

API and worker are separate containers sharing a model-weights volume. Models are loaded once per process and held resident.

### 15.3 Environments

| Environment | Purpose | Data |
|---|---|---|
| `local` | Development | Docker Compose; recorded ATS fixtures |
| `staging` | Pre-release verification | Synthetic CVs only; real job data |
| `production` | Live | Real data; restricted access; audit logging on |

Real CVs never enter staging. Synthetic profiles are generated for testing.

### 15.4 Release process

1. Feature branch → PR
2. CI: ruff, mypy strict, pytest unit + integration, import-linter
3. Evaluation gate on any change to matching, scoring, or model config
4. Alembic migration reviewed for backward compatibility (expand → migrate → contract)
5. Merge to `main` → build images → deploy worker → deploy API
6. Post-deploy smoke test against `/health` and one live match run

Database migrations are always backward-compatible for one release, so API and worker can be deployed independently.

---

## 16. Security, Privacy & Compliance

### 16.1 Data classification

| Class | Examples | Controls |
|---|---|---|
| **Sensitive personal** | CV binaries, raw CV text, contact details | Encrypted at rest, access-logged, redacted before hosted inference, 30-day purge on deletion |
| **Personal** | Profile, applications, engagement events | Encrypted at rest, exportable, deletable |
| **Third-party content** | Job descriptions | Retained for processing, never republished in full |
| **Operational** | Logs, metrics, source health | PII-scrubbed, 30-day retention |

### 16.2 Controls

- **Transport:** TLS 1.3 only; HSTS.
- **At rest:** volume encryption; CV objects encrypted with a per-tenant key.
- **Authentication:** Argon2id password hashing; JWT access tokens (15 min) with rotating refresh tokens (30 days).
- **Authorisation:** every query scoped by `user_id` at the repository layer, not the route layer.
- **Redaction:** name, email, phone, and address are stripped from CV text before it is sent to any hosted model. The mapping is held in memory only for the duration of the request.
- **Secrets:** environment-injected; never committed; rotated quarterly.
- **Audit:** every access to a CV, every export, and every deletion writes to `audit_log`.

### 16.3 Regulatory posture

**GDPR.** CVs frequently contain nationality, photographs, marital status, and occasionally health or religious information, placing them in Article 9 special-category territory. The system applies data minimisation (only declared fields are extracted), purpose limitation (data is used solely for matching), storage limitation (defined retention), and supports the rights of access (`GET /me/export`), rectification (`PATCH /profile`), and erasure (`DELETE /me`).

**Egypt PDPL (Law 151/2018).** The law applies to controllers processing personal data electronically and contemplates registration and licensing with the Data Protection Centre. Executive regulations have been slow to materialise and the practical enforcement position remains uncertain. **Action item: obtain a written opinion from an Egyptian data-protection lawyer before onboarding the second user.** Nothing in this document constitutes legal advice.

**Third-party terms.** Every source in the registry carries a `DATA_SOURCES.md` entry recording the terms reviewed, the date of review, and the conduct constraints applied. Sources whose terms prohibit programmatic access are not added, regardless of technical feasibility.

**Copyright.** Job descriptions are third-party copyrighted text. They are stored for processing and displayed as short excerpts with a link to the source. Full descriptions are not republished.

### 16.4 Compliance checkpoints

| Trigger | Requirement |
|---|---|
| Before second user | Legal opinion on PDPL; privacy policy published; consent flow live |
| Before any outbound email | Unsubscribe mechanism, sender identification, consent record |
| Before adding any source | Terms review documented in `DATA_SOURCES.md` |
| Before public launch | DPIA completed; retention schedule enforced in code |

---

## 17. Observability & Service Levels

### 17.1 Golden signals

| Signal | Metric | Alert threshold |
|---|---|---|
| **Data freshness** | Median age of newest posting per source | > 6 hours |
| **Source health** | Rows returned per run vs. 7-day median | < 20% of median |
| **Link integrity** | Share of displayed postings verified within 48 h | < 98% |
| **Match quality** | NDCG@10 on golden set, weekly | Drop > 2 points |
| **Groundedness** | Extraction hallucination rate | > 2% |
| **Inference budget** | Tokens consumed vs. daily allowance | > 80% by 18:00 |
| **Latency** | p95 `/matches` response | > 800 ms |
| **Errors** | 5xx rate | > 0.5% |

### 17.2 Instrumentation

- Structured JSON logging with `request_id` propagated to worker tasks.
- Per-source dashboards: fetched, new, deduplicated, errors, latency.
- Every LLM call records model, tokens in/out, latency, and validation outcome to `model_runs`.
- Sentry for exceptions; a weekly digest of the top failure modes.

### 17.3 Service objectives (post-launch)

| Objective | Target |
|---|---|
| API availability | 99.0% monthly |
| Displayed postings resolving to a live page | ≥ 98% |
| Digest delivery within 30 minutes of schedule | ≥ 99% |
| Matching run completion for an active profile | ≥ 99% nightly |

---

## 18. Delivery Plan

Single engineer, approximately 15 hours per week. Sixteen weeks to a complete, differentiated product; six weeks to a defensible one.

### Phase 0 — Data Spine · Weeks 1–2

**Objective:** a job corpus that is real, fresh, deduplicated, and correctly linked.

| Deliverable | Detail |
|---|---|
| Schema and migrations | Full DDL from §6, Alembic-managed |
| Source adapters | Greenhouse, Lever, Ashby, Workable |
| Board registry | 150 curated boards across four YAML files |
| Normalisation | Per-adapter mapping to `NormalizedJob` |
| Deduplication | Five-stage pipeline with union-find clustering |
| Company resolution | Alias table plus fuzzy matching, review queue |
| Discovery worker | Hourly, rate-limit-aware, health-checked |

**Exit criteria:** ≥ 5,000 unique live postings; every row has a populated `apply_url`; per-source health dashboard operational; Lever's bare-array response handled correctly (regression test in place).

### Phase 1 — Matching Core · Weeks 3–4

**Objective:** a ranked shortlist that is genuinely useful to one real user.

| Deliverable | Detail |
|---|---|
| CV ingestion | PDF/DOCX extraction, parse-quality scoring |
| Profile extraction | Schema-constrained, evidence-span verified |
| Skill taxonomy | Seed vocabulary with aliases (EN + AR) |
| Embedding pipeline | Document and sentence level, batched |
| Gates | Work auth, location, seniority, language, freshness |
| Hybrid retrieval | BM25 + pgvector, RRF fusion |
| Reranking | Local cross-encoder on top-120 |
| LLM analysis | Requirement extraction and assessment on top-25 |
| Scoring | Decomposed sub-scores, percentile presentation |

**Exit criteria:** the operator finds, through the system, at least one role they would have missed and would genuinely apply to.

### Phase 2 — Evaluation · Weeks 5–6 *(non-negotiable)*

**Objective:** convert every subsequent decision from opinion into measurement.

| Deliverable | Detail |
|---|---|
| Rubric | Written 0–3 grading criteria |
| Golden set | 300 stratified pairs, 3 annotators, κ reported |
| Metrics module | NDCG@10, MRR, P@5, recall of grade-3 |
| Ablation study | Four configurations with latency |
| Negative controls | Cross-domain, seniority monotonicity, shuffled pairs |
| Dedup evaluation | 200 hand-labelled pairs; precision and recall |
| Hallucination measurement | 100 verified extractions |
| CI regression gate | Build fails on > 2-point NDCG drop |

**Exit criteria:** published ablation table; κ ≥ 0.60; NDCG@10 ≥ 0.75; hallucination rate < 2%.

> **This phase is where the project becomes credible.** Skipping it produces another undifferentiated job-agent repository. Completing it produces evidence.

### Phase 3 — Product · Weeks 7–9

Multi-user authentication and authorisation · URL verification worker · Next.js shortlist and evidence UI · save/dismiss/apply tracking · duplicate-application guard · daily digest · data export and hard deletion · privacy policy and consent flow.

**Exit criteria:** three non-technical users complete a week of unassisted use; ≥ 98% link integrity.

### Phase 4 — Candidate Tooling · Weeks 10–12

CV parseability report · gap analysis with concrete learning suggestions · tailored bullet rewrites gated by the anti-invention diff · cover-letter generation from structured fact bundles · interview preparation derived from the posting's actual extracted requirements.

**Exit criteria:** anti-invention diff blocks 100% of a 50-case adversarial test set.

### Phase 5 — Bilingual Pipeline · Weeks 13–15

Arabic CV parsing · Arabic and mixed-script job-description normalisation · cross-lingual matching evaluation with a dedicated labelled split · Arabic UI locale.

**Exit criteria:** NDCG@10 on the Arabic split within 5 points of the English split. This is the product's clearest differentiator and its most defensible technical contribution.

### Phase 6 — Personalisation · Week 16 onward

Interpretable per-user weight adjustment from implicit engagement signals, gated on 200+ events and validated per user against that user's own labelled subset.

### 18.1 Milestone summary

| Milestone | Week | Definition of done |
|---|---|---|
| M1 — Corpus live | 2 | 5,000 deduplicated postings, valid apply URLs |
| M2 — First useful shortlist | 4 | Operator applies to a discovered role |
| M3 — Measured system | 6 | Ablation table published; CI gate active |
| M4 — Multi-user product | 9 | Three external users, one week unassisted |
| M5 — Candidate tooling | 12 | Anti-invention guarantee verified |
| M6 — Bilingual | 15 | Arabic split within 5 NDCG points |

### 18.2 Minimum defensible scope

If work stops after **Week 6**, the project still comprises: a legally sourced, deduplicated, verified job corpus; an explainable hybrid retrieval and scoring pipeline; and a published evaluation with ablations and groundedness measurement. That is a complete piece of engineering. Everything after Week 6 is product surface.

---

## 19. Risk Register

| # | Risk | Likelihood | Impact | Early indicator | Response |
|---|---|---|---|---|---|
| R1 | Inference rate limits bind during nightly matching | High | Medium | Sustained 429s after 02:00 | Reduce funnel top-N; batch; prompt-cache the system prompt; stagger users |
| R2 | Source adapter breaks silently | High | High | Run returns zero rows against a productive 7-day median | Per-source health alerts from day one; recorded fixtures in CI |
| R3 | Workable widget endpoint changes without notice | Medium | Medium | Schema validation failure | Treat as untrusted; isolate; degrade gracefully to other sources |
| R4 | Displayed apply links are dead | Medium | High | User report or verification failure rate | Verification worker in Phase 3, not later; suppress unverified postings |
| R5 | Company entity resolution produces false merges | Medium | Medium | Dedup precision below 0.95 | Human review queue; conservative merge threshold; alias curation |
| R6 | PDPL/GDPR exposure on multi-user launch | Medium | High | Second user onboards | Legal opinion before M4; consent flow and DPIA as launch blockers |
| R7 | Embedding model change invalidates the index | Low | High | Model deprecation notice | `model` column on every embedding; dual-write migration path |
| R8 | Scope creep back toward the 22-module design | High | High | Outreach or auto-apply work appears in a sprint | Change control in §3.3; re-read the anti-goals table |
| R9 | Golden-set annotator disagreement | Medium | Medium | κ < 0.60 | Revise rubric before trusting any metric; do not proceed |
| R10 | Free-tier host withdrawal | Low | High | Provider notice | Containerised deployment; migration to a €5/month VPS is a configuration change |

---

## 20. Success Metrics

### 20.1 Data quality — the foundation

| Metric | Target |
|---|---|
| Unique live postings in corpus | ≥ 5,000 by Week 2; ≥ 20,000 by Week 12 |
| Displayed postings resolving to a live page | ≥ 98% |
| Median posting age at first display | ≤ 6 hours |
| Deduplication precision / recall | ≥ 0.95 / ≥ 0.90 |
| Postings with a verified canonical apply URL | 100% |

### 20.2 Model quality

| Metric | Target |
|---|---|
| NDCG@10 (English golden set) | ≥ 0.75 |
| NDCG@10 (Arabic split) | within 5 points of English |
| Precision@5 | ≥ 0.70 |
| Extraction hallucination rate | < 2% |
| Anti-invention diff catch rate | 100% on adversarial set |

### 20.3 Product

| Metric | Target |
|---|---|
| Digest open rate | ≥ 40% |
| Save-or-apply rate on top 5 | ≥ 25% |
| Dismiss rate on top 5 | ≤ 30% |
| Weekly active retention at 4 weeks | ≥ 50% of onboarded users |

### 20.4 Operational

| Metric | Target |
|---|---|
| Monthly infrastructure cost at 50 users | $0 |
| p95 `/matches` latency | ≤ 800 ms |
| Nightly matching completion rate | ≥ 99% |

### 20.5 Outcome

At least one interview obtained by a real user through a role the system surfaced. This is the only metric that validates the premise, and no other number substitutes for it.

---

## 21. Appendices

### Appendix A — Glossary

| Term | Definition |
|---|---|
| **ATS** | Applicant Tracking System — the software an employer uses to publish roles and manage candidates |
| **Board token** | The employer-specific identifier in an ATS public job-board URL |
| **Blocking** | Restricting duplicate comparison to candidates sharing a key, to avoid quadratic cost |
| **Cross-encoder** | A model scoring a query–document pair jointly; more accurate and slower than bi-encoder cosine |
| **Evidence span** | Character offsets into a source document justifying an extracted claim |
| **Gate** | A boolean eligibility rule evaluated before scoring; failure excludes the posting |
| **Golden set** | A hand-labelled benchmark used to measure ranking quality |
| **κ (Cohen's kappa)** | Inter-annotator agreement corrected for chance |
| **NDCG@k** | Normalised Discounted Cumulative Gain — graded ranking quality at cut-off k |
| **RRF** | Reciprocal Rank Fusion — combines rankings from independent retrievers |
| **SimHash** | Locality-sensitive hash for near-duplicate text detection |

### Appendix B — ATS endpoint reference

```bash
# Greenhouse — documented
curl "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true"
# → {"jobs": [{id, title, location.name, absolute_url, updated_at, departments, offices, content}]}

# Lever — documented · NOTE: bare array, title field is `text`
curl "https://api.lever.co/v0/postings/{company}?mode=json"
# → [{id, text, categories, hostedUrl, createdAt, descriptionPlain}]

# Ashby — documented · publishes structured compensation
curl "https://api.ashbyhq.com/posting-api/job-board/{org}"
# → {"jobs": [{id, title, location, employmentType, secondaryLocations, jobUrl}]}

# Workable — widget endpoint, undocumented, monitor closely
curl "https://apply.workable.com/api/v1/widget/accounts/{sub}?details=true"
# → {"jobs": [{shortcode, title, location, url, description}]}

# SmartRecruiters
curl "https://api.smartrecruiters.com/v1/companies/{id}/postings"

# Recruitee
curl "https://{company}.recruitee.com/api/offers/"
```

**Integration rule:** never write `data.get("jobs", [])` as a shared helper. Each adapter owns its container shape and its field vocabulary. A shared accessor is how Lever silently returns zero rows for a week.

### Appendix C — ATS detection precedence

| Order | Method | Confidence | Signal |
|---|---|---|---|
| 1 | By construction | 1.00 | Posting was fetched from that ATS's own API |
| 2 | URL host pattern | 0.95 | `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com`, `*.myworkdayjobs.com`, `*.icims.com`, `*.taleo.net`, `jobs.smartrecruiters.com`, `*.breezy.hr`, `jobs.jobvite.com`, `*.recruitee.com`, `*.teamtailor.com`, `*.bamboohr.com/careers`, `*.personio.de`, `*.oraclecloud.com/hcmUI`, `career*.successfactors.*` |
| 3 | HTML fingerprint | 0.80 | Script hosts, `meta[name=generator]`, JSON-LD `JobPosting`, known DOM IDs such as `#grnhse_app` |
| 4 | Redirect terminus | 0.75 | Re-apply rules 2–3 to the final URL after following redirects |
| 5 | Unresolved | — | `ats_platform = 'unknown'` — a valid, honest value |

### Appendix D — Apply-URL resolution precedence

1. ATS-native field (`absolute_url`, `hostedUrl`, `jobUrl`)
2. `<link rel="canonical">`
3. JSON-LD `JobPosting.url`
4. Final URL after resolving the redirect chain
5. `source_url` as last resort

Tracking parameters stripped: `utm_*`, `gh_src`, `ref`, `fbclid`, `gclid`, `source`.
`source_url`, `canonical_url`, and `apply_url` are all persisted. None is ever overwritten by a later resolution.

### Appendix E — Configuration surface

```yaml
# config.yaml — every tunable in one place, none hardcoded
scoring:
  weights:
    skill_coverage:        0.35
    requirement_alignment: 0.25
    seniority_fit:         0.15
    semantic_similarity:   0.15
    freshness:             0.10
  freshness_half_life_days: 7
  skill_fuzzy_threshold:    0.82

gates:
  max_years_shortfall:   2
  max_posting_age_days:  45
  require_verified_url:  true

funnel:
  retrieval_top_k: 200
  rrf_k:           60
  fusion_top_n:    120
  rerank_top_n:     25

models:
  embedding: intfloat/multilingual-e5-small
  reranker:  BAAI/bge-reranker-base
  extraction: { provider: groq, model: llama-3.1-8b-instant }
  analysis:   { provider: groq, model: llama-3.3-70b }
  fallbacks:  [gemini, ollama]

dedup:
  simhash_hamming_max: 3
  semantic_threshold:  0.94
  title_block_tokens:  3

verification:
  reverify_after_hours: 12
  suppress_after_hours: 48
  per_host_min_interval_seconds: 2
```

### Appendix F — Architecture decision records

| ADR | Decision | Rationale |
|---|---|---|
| 0001 | Modular monolith over microservices | Single operator; package boundaries provide the isolation that matters without distributed-systems cost |
| 0002 | ATS-first sourcing over aggregator-first | Freshness, legality, authoritative apply URLs, and ATS platform known by construction |
| 0003 | Decomposed scoring over end-to-end similarity | Explainability, gate enforcement, and per-requirement evidence |
| 0004 | No outcome-based learning-to-rank | Labels sparse, censored by non-response, delayed, and confounded; would learn noise |
| 0005 | Postgres queue over Redis at V1 | Durability, transactional consistency with domain data, one fewer service |
| 0006 | Percentile presentation over absolute scores | Self-calibrating; resistant to misreading as hire probability |

---

*End of document. Version 1.0 — 8 September 2026.*
