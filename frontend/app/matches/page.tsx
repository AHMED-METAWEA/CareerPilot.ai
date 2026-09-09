import Link from "next/link";
import { redirect } from "next/navigation";
import { MatchCard } from "@/components/MatchCard";
import { api, ApiError, isSignedIn, type MatchCardData } from "@/lib/api";

type MatchesResponse = {
  matches: MatchCardData[];
  pool_size: number;
  next_cursor: number | null;
};

export default async function MatchesPage({
  searchParams,
}: {
  searchParams: Promise<{ min?: string; remote?: string }>;
}) {
  if (!(await isSignedIn())) redirect("/login");
  const { min, remote } = await searchParams;

  const query = new URLSearchParams();
  if (min) query.set("min_percentile", min);
  if (remote === "true") query.set("remote", "true");

  let data: MatchesResponse;
  try {
    data = await api<MatchesResponse>(`/api/v1/matches?${query}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login");
    throw error;
  }

  if (data.matches.length === 0) {
    return <EmptyState poolSize={data.pool_size} />;
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

      <div className="mt-4 flex gap-2 text-xs">
        <Filter href="/matches" label="All" active={!min && remote !== "true"} />
        <Filter href="/matches?min=90" label="Top 10%" active={min === "90"} />
        <Filter href="/matches?remote=true" label="Remote only" active={remote === "true"} />
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

function EmptyState({ poolSize }: { poolSize: number }) {
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
