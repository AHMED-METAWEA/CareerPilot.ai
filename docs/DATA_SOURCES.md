# Data sources — terms, conduct and review status

> Every source in the registry has an entry here before it is enabled (§16.3).
> An entry records what the source is, what conduct constraints apply, and
> whether a terms review has actually been done — not whether one is intended.

**Review status is deliberately honest.** Nothing below claims a legal review
that has not happened. `pending` means exactly that, and §16.4 makes a
documented terms review a precondition for adding a source, not for keeping one.

---

## Tier 1 — employer applicant-tracking systems

These are keyless public JSON endpoints that ATS vendors publish so third
parties can render employer job boards. They are documented, stable, intended
for consumption, and authoritative for the employer's own postings (§5.1).

| Platform | Endpoint | Auth | Documented | Terms reviewed | Conduct applied |
|---|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | yes | pending | 20 rpm, 2 s/host, conditional GET |
| Lever | `api.lever.co/v0/postings/{company}?mode=json` | none | yes | pending | 20 rpm, 2 s/host |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{org}` | none | yes | pending | 20 rpm, 2 s/host |
| Workable | `apply.workable.com/api/v1/widget/accounts/{sub}?details=true` | none | **no — widget endpoint** | pending | 20 rpm, 2 s/host, schema-validated, monitored aggressively |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | none | yes | pending | 20 rpm, 2 s/host, bodies fetched in a separate bounded phase |
| Recruitee | `{company}.recruitee.com/api/offers/` | none | yes | pending | 20 rpm, 2 s/host |

### Workable — treated as untrusted

The Workable endpoint is the one their embeddable careers widget calls, not
their documented authenticated API. It is public and functional, but it can
change without notice (R3). Three consequences are implemented in code:

1. `WorkableAdapter.fetch` validates the container shape and raises on anything
   unexpected, so a change surfaces as a failed run rather than zero rows.
2. The source is monitored against its own 7-day median like every other, and a
   collapse in volume marks the run `suspect`.
3. Five consecutive failures open the circuit breaker and disable the source
   rather than retrying into a rate limit.

## Tier 2 — aggregators (not yet enabled)

Adapters for Adzuna, Arbeitnow, Remotive, RemoteOK and USAJOBS are specified in
§5.3 and are **not** part of the Phase 0 build. Each requires its own entry here
before it is added.

| Source | Free allowance | Note |
|---|---|---|
| Adzuna | ~1,000 calls/month | API key; strong salary normalisation |
| Arbeitnow | unauthenticated | exposes `visa_sponsorship` — high value for MENA-based candidates |
| Remotive | unauthenticated | clean JSON, moderate volume |
| RemoteOK | unauthenticated | overlaps heavily with Remotive; dedup essential |
| USAJOBS | free with key | narrow but authoritative |

## Permanently excluded

| Source | Reason |
|---|---|
| LinkedIn | Scraping breaches the terms of service irrespective of the computer-misuse questions explored in *hiQ Labs v. LinkedIn*, where LinkedIn ultimately prevailed on a breach-of-contract theory |
| Indeed | Publisher API retired in 2023; any guidance recommending it is stale |
| Glassdoor | Terms of service |

These are product boundaries, not roadmap items (§1.4).

## The MENA coverage gap

Wuzzuf, Bayt, Forasna and Tanqeeb — the dominant Egyptian and Gulf platforms —
publish no open public API, and Bayt operates a partner programme requiring a
commercial agreement. Version 1 covers the region **indirectly**: funded
regional employers and multinational delivery centres frequently run Workable,
Recruitee or Greenhouse, and are reachable through Tier 1. Direct local-platform
coverage requires a partnership and is a commercial workstream, not an
engineering task (§5.5).

## Crawling conduct (§5.6)

Implemented once, in `app/adapters/http.py`, so no adapter can forget it:

- `robots.txt` fetched, cached 24 h, and honoured (`RobotsCache`).
- Descriptive `User-Agent` including a contact URL on every request.
- Per-host concurrency of 1 with a minimum 2-second interval.
- Exponential backoff honouring `Retry-After` on 429/503.
- Conditional requests (`If-None-Match`, `If-Modified-Since`) where supported.
- No authentication wall is circumvented and no anti-bot measure is defeated.

## Copyright

Job descriptions are third-party copyrighted text. They are stored for
processing and displayed as short excerpts with a link to the source. Full
descriptions are never republished (§16.3).

## Registry as built

141 boards, all probed live before being committed (550 candidates were tried):

| Adapter | Boards |
|---|---|
| Greenhouse | 73 |
| Ashby | 45 |
| Lever | 10 |
| Recruitee | 6 |
| SmartRecruiters | 4 |
| Workable | 3 |

| Region file | Boards |
|---|---|
| `global_remote.yaml` | 110 |
| `eu_remote.yaml` | 27 |
| `mena.yaml` | 3 |
| `egypt.yaml` | 1 |

The regional skew is the coverage gap above, not an oversight: employer board
tokens for Egyptian and Gulf companies are largely unguessable, and the ones
tried did not resolve. Closing it means curating tokens from employers' actual
careers pages, which is data work.

## Adding a source

1. Add an entry to this file, including the terms position and the conduct
   constraints that apply.
2. Add the board to `backend/config/boards/*.yaml`.
3. Probe it: `careerpilot boards probe <adapter> <token>`.
4. Sync: `careerpilot sources sync`.

A board token is a claim about the world, and claims decay: companies change
ATS vendors and rename boards. `.github/workflows/boards.yml` re-probes the whole
registry weekly.
