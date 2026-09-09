import Link from "next/link";
import type { MatchCardData } from "@/lib/api";

/**
 * One row of the shortlist (§11.6 step 2).
 *
 * The score is shown as a percentile within the candidate's own pool, never as
 * an absolute figure and never as a probability of any outcome (§8.5, ADR 0006).
 * The verification time is on the card because "this link worked an hour ago"
 * is the product's most concrete promise.
 */
export function MatchCard({ match }: { match: MatchCardData }) {
  const location =
    match.locations?.map((l) => l.raw ?? l.city).filter(Boolean).join(" · ") ||
    (match.remote_type === "remote" ? "Remote" : null);

  return (
    <article className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium">
            <Link href={`/matches/${match.match_id}`} className="hover:underline">
              {match.title}
            </Link>
          </h2>
          <p className="mt-0.5 text-sm text-[var(--color-ink-soft)]">
            {[match.company, location, match.remote_type].filter(Boolean).join(" · ")}
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-[var(--color-accent-soft)] px-3 py-1 text-xs font-medium text-[var(--color-accent)]">
          {match.presentation.band}
        </span>
      </div>

      {match.explanation ? (
        <p className="mt-3 text-sm text-[var(--color-ink-soft)]">{match.explanation}</p>
      ) : null}

      {match.gaps.length > 0 ? (
        <p className="mt-2 text-sm">
          <span className="text-[var(--color-ink-soft)]">Gaps: </span>
          {match.gaps.slice(0, 3).join(", ")}
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-[var(--color-ink-soft)]">
        <Verification status={match.url_status} at={match.last_verified_at} />
        {match.posted_at ? <span>Posted {relative(match.posted_at)}</span> : null}
        <Link href={`/matches/${match.match_id}`} className="ml-auto underline">
          See the evidence
        </Link>
      </div>
    </article>
  );
}

export function Verification({ status, at }: { status: string; at: string | null }) {
  if (status === "live") {
    return (
      <span className="text-[var(--color-met)]">
        Link checked{at ? ` ${relative(at)}` : ""}
      </span>
    );
  }
  if (status === "redirected") return <span>Link redirects — check where it lands</span>;
  if (status === "gone") return <span className="text-[var(--color-missing)]">Posting closed</span>;
  return <span>Not verified recently</span>;
}

export function relative(iso: string): string {
  const then = new Date(iso).getTime();
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}
