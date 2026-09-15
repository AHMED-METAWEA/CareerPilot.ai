import Link from "next/link";
import { redirect } from "next/navigation";
import { MatchCard } from "@/components/MatchCard";
import { api, ApiError, isSignedIn, type MatchCardData, type MatchList } from "@/lib/api";

export default async function MatchesPage({
  searchParams,
}: {
  searchParams: Promise<{
    min?: string;
    remote?: string;
    location?: string;
    kind?: string;
  }>;
}) {
  if (!(await isSignedIn())) redirect("/login");
  const { min, remote, location, kind } = await searchParams;

  const query = new URLSearchParams();
  if (min) query.set("min_percentile", min);
  if (remote === "true") query.set("remote", "true");
  if (location) query.set("location", location);
  if (kind) query.set("kind", kind);

  let data: MatchList;
  try {
    data = await api<MatchList>(`/api/v1/matches?${query}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login");
    throw error;
  }

  if (data.matches.length === 0) {
    return (
      <EmptyState
        poolSize={data.pool_size}
        refreshing={data.refresh_in_progress}
        filtered={Boolean(location || kind || min || remote === "true")}
      />
    );
  }

  return (
    <div>
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Your shortlist</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
            {data.matches.length} of {data.pool_size.toLocaleString()} roles reviewed for you.
            Every link below was checked against the employer&apos;s own page.
          </p>
        </div>
        <Link href="/matches/withheld" className="text-sm underline">
          What was held back, and why
        </Link>
      </header>

      {/* Two independent axes, kept on separate rows so it is obvious they
          combine rather than replace one another. The querystring carries both,
          so a filtered list is a link somebody can bookmark or send. */}
      <div className="mt-4 space-y-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[var(--color-ink-soft)]">Where</span>
          <Filter href={withFilters({ kind, min, remote })} label="Anywhere" active={!location} />
          <Filter
            href={withFilters({ kind, min, remote, location: "Egypt" })}
            label="Egypt"
            active={location === "Egypt"}
          />
          <Filter
            href={withFilters({ kind, min, remote: "true" })}
            label="Remote"
            active={remote === "true"}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[var(--color-ink-soft)]">What</span>
          <Filter href={withFilters({ location, min, remote })} label="Everything" active={!kind} />
          <Filter
            href={withFilters({ location, min, remote, kind: "job" })}
            label="Jobs"
            active={kind === "job"}
          />
          <Filter
            href={withFilters({ location, min, remote, kind: "internship" })}
            label="Internships"
            active={kind === "internship"}
          />
          <Filter
            href={withFilters({ location, remote, kind, min: "90" })}
            label="Top 10%"
            active={min === "90"}
          />
        </div>
      </div>

      <div className="mt-5 space-y-4">
        {data.matches.map((match) => (
          <MatchCard key={match.match_id} match={match} />
        ))}
      </div>
    </div>
  );
}

function Filter({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <Link
      href={href}
      className={`rounded-full border px-3 py-1 ${
        active
          ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
          : "border-[var(--color-line)] text-[var(--color-ink-soft)]"
      }`}
    >
      {label}
    </Link>
  );
}

function withFilters(next: Record<string, string | undefined>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(next)) {
    if (value) query.set(key, value);
  }
  const rendered = query.toString();
  return rendered ? `/matches?${rendered}` : "/matches";
}

function EmptyState({
  poolSize,
  refreshing,
  filtered = false,
}: {
  poolSize: number;
  refreshing: boolean;
  filtered?: boolean;
}) {
  // A filtered list that comes back empty is not an empty shortlist, and
  // telling someone to upload a CV because they asked for Egypt would be the
  // same mistake in a new place.
  if (filtered && poolSize > 0) {
    return (
      <div className="mx-auto max-w-lg py-10 text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Nothing here yet</h1>
        <p className="mt-3 text-sm text-[var(--color-ink-soft)]">
          Your shortlist has {poolSize.toLocaleString()} roles, but none match this filter.
          Egyptian and internship postings are a small share of what the sources publish —
          the corpus holds far more remote and European roles than local ones.
        </p>
        <Link href="/matches" className="mt-6 inline-block text-sm underline">
          Show everything
        </Link>
      </div>
    );
  }

  // An empty list has three quite different meanings, and telling a candidate
  // the wrong one is worse than telling them nothing. Someone whose run is
  // still going was previously shown "No matches yet — Upload a CV", which is
  // both wrong and the one instruction they had already followed.
  if (refreshing) {
    return (
      <div className="mx-auto max-w-lg py-10 text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Building your shortlist</h1>
        <p className="mt-3 text-sm text-[var(--color-ink-soft)]">
          We are reading your CV against every live opening and checking each requirement
          against what your CV actually says. This usually takes a few minutes.
        </p>
        <p className="mt-2 text-sm text-[var(--color-ink-soft)]">
          Nothing is lost if you leave this page — reload it when you come back.
        </p>
        <form action="/matches" method="get" className="mt-6">
          <button className="rounded-md border border-[var(--color-line)] px-4 py-2 text-sm">
            Reload
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-lg py-10 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">No matches yet</h1>
      <p className="mt-3 text-sm text-[var(--color-ink-soft)]">
        {poolSize === 0
          ? "Upload a CV and we will work through the live openings for you. Matching runs nightly, and you can trigger it yourself."
          : "Nothing new has cleared the eligibility gates since the last run."}
      </p>
      <div className="mt-6 flex justify-center gap-3">
        <Link
          href="/onboarding"
          className="rounded-md bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white"
        >
          Upload a CV
        </Link>
        <form action="/api/matches/refresh" method="post">
          <button className="rounded-md border border-[var(--color-line)] px-4 py-2 text-sm">
            Run matching now
          </button>
        </form>
      </div>
    </div>
  );
}
