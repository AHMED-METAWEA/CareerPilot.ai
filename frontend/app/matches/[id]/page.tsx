import Link from "next/link";
import { redirect } from "next/navigation";
import { EvidencePanel } from "@/components/EvidencePanel";
import { GapReport } from "@/components/GapReport";
import { relative, Verification } from "@/components/MatchCard";
import { api, isSignedIn, type MatchDetail } from "@/lib/api";

/**
 * The evidence view (§11.6 step 3) — the page the whole system exists to
 * produce.
 *
 * Everything shown here is traceable: each requirement cites the CV line that
 * answered it, the score decomposes into its named terms, and the apply link is
 * the employer's own, verbatim (§12.7). No absolute score is displayed, and
 * nothing is described as a chance of an outcome (ADR 0006).
 */
export default async function MatchDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  if (!(await isSignedIn())) redirect("/login");
  const { id } = await params;

  const match = await api<MatchDetail>(`/api/v1/matches/${id}`);
  const location =
    match.locations?.map((l) => l.raw ?? l.city).filter(Boolean).join(" · ") ||
    (match.remote_type === "remote" ? "Remote" : null);

  return (
    <div className="space-y-6">
      <Link href="/matches" className="text-sm underline">
        ← Back to your shortlist
      </Link>

      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{match.title}</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
          {[match.company, location, match.remote_type].filter(Boolean).join(" · ")}
        </p>
        <p className="mt-3 text-sm">{match.presentation.summary}</p>
      </header>

      <div className="flex flex-wrap items-center gap-4 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4 text-sm">
        <div className="text-xs text-[var(--color-ink-soft)]">
          <Verification status={match.url_status} at={match.last_verified_at} />
          {match.posted_at ? <span> · Posted {relative(match.posted_at)}</span> : null}
        </div>
        <div className="ml-auto flex gap-2">
          <form action={`/api/jobs/${match.posting_id}/save`} method="post">
            <button className="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm">
              Save
            </button>
          </form>
          <form action={`/api/jobs/${match.posting_id}/dismiss`} method="post">
            <button className="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm">
              Not for me
            </button>
          </form>
          <Link
            href={`/matches/${match.match_id}/prepare`}
            className="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm"
          >
            Prepare
          </Link>
          {/* The apply link is the employer's own, opened in a new tab. We do
              not submit anything, and we do not wrap or shorten the URL. */}
          <a
            href={match.apply_url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white"
          >
            Apply on the employer&apos;s site ↗
          </a>
        </div>
      </div>

      <GapReport
        gaps={match.gaps}
        matched={match.explanation ?? "Scored on the requirements we could extract."}
      />

      <section>
        <h2 className="text-lg font-medium">Requirement by requirement</h2>
        <p className="mb-4 mt-1 text-sm text-[var(--color-ink-soft)]">
          Each line below is a requirement from the posting, with the sentence from your CV that
          answered it.
        </p>
        <EvidencePanel requirements={match.requirements} />
      </section>

      <ScoreBreakdown subscores={match.subscores} contributions={match.contributions} />

      <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
        <h2 className="text-sm font-medium">From the posting</h2>
        <p className="mt-2 whitespace-pre-line text-sm text-[var(--color-ink-soft)]">
          {match.excerpt}…
        </p>
        <p className="mt-3 text-xs text-[var(--color-ink-soft)]">
          An excerpt only. Read the full description on the employer&apos;s page.
        </p>
      </section>
    </div>
  );
}

const TERM_LABELS: Record<string, string> = {
  skill_coverage: "Skills the posting asks for",
  requirement_alignment: "How closely your CV answers each requirement",
  seniority_fit: "Seniority",
  semantic_similarity: "Overall closeness of the role to your experience",
  freshness: "How recently it was posted",
};

function ScoreBreakdown({
  subscores,
  contributions,
}: {
  subscores: Record<string, number>;
  contributions: Record<string, number>;
}) {
  const terms = Object.keys(TERM_LABELS).filter((key) => key in subscores);
  const unassessed = terms.filter((key) => !(key in contributions));

  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <h2 className="text-sm font-medium">How this was scored</h2>
      <p className="mt-1 text-xs text-[var(--color-ink-soft)]">
        Five named terms, combined by fixed weights. No model produces any of these numbers.
      </p>

      <ul className="mt-4 space-y-2">
        {terms.map((key) => {
          const value = subscores[key] ?? 0;
          const assessed = key in contributions;
          return (
            <li key={key} className="text-sm">
              <div className="flex items-baseline justify-between gap-3">
                <span className={assessed ? "" : "text-[var(--color-ink-soft)] line-through"}>
                  {TERM_LABELS[key]}
                </span>
                <span className="text-xs text-[var(--color-ink-soft)]">
                  {assessed ? `${Math.round(value * 100)}%` : "not assessable"}
                </span>
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-[var(--color-line)]">
                <div
                  className={`h-full rounded-full ${
                    assessed ? "bg-[var(--color-accent)]" : "bg-[var(--color-line)]"
                  }`}
                  style={{ width: `${Math.round((assessed ? value : 0) * 100)}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>

      {unassessed.length > 0 ? (
        <p className="mt-4 text-xs text-[var(--color-ink-soft)]">
          This posting did not give us enough to judge{" "}
          {unassessed.map((key) => TERM_LABELS[key]?.toLowerCase()).join(" or ")}. The match is
          scored only on what could be assessed, so it cannot reach the top of your list on the
          strength of what we could not read.
        </p>
      ) : null}
    </section>
  );
}
