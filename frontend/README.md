# CareerPilot web client

Next.js 15 (App Router) + TypeScript + Tailwind v4. Server components call the
API directly; the browser never holds an access token.

## Running

```bash
npm install
CAREERPILOT_API_URL=http://localhost:8000 npm run dev
```

The API must be running (`make api` in the repository root).

## How authentication works here

Tokens are issued by the API and stored in **httpOnly cookies** set by the route
handlers under `app/api/auth/`. Client JavaScript never sees them, so an XSS bug
on this origin cannot take a session with it (§16.2). Every page that needs data
is a server component: it reads the cookie, calls the API, and renders — there
is no client-side API base URL and no token in `localStorage`.

## The pages that matter

| Route | What it is for |
|---|---|
| `/onboarding` | CV upload, the parseability report, and what was extracted |
| `/matches` | The shortlist, percentile-presented |
| `/matches/[id]` | The evidence view — each requirement with the CV line that answered it |
| `/matches/withheld` | Roles a gate excluded, and why |
| `/applications` | Application tracking, including "no response" as a real status |
| `/settings` | Consent, export, deletion |

## Two rules this client enforces

1. **No absolute score is ever displayed.** Matches are shown as a percentile
   within the candidate's own pool, and no copy describes a score as a chance of
   an interview or an offer (§8.5, ADR 0006).
2. **The apply link is the employer's own, verbatim.** It is never wrapped,
   shortened or proxied (§12.7), and it opens in a new tab. Nothing is submitted
   on the candidate's behalf.
