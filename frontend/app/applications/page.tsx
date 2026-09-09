import { redirect } from "next/navigation";
import { relative, Verification } from "@/components/MatchCard";
import { api, isSignedIn, type ApplicationRow } from "@/lib/api";

const STATUSES = [
  "applied",
  "screening",
  "interviewing",
  "offer",
  "rejected",
  "no_response",
  "withdrawn",
];

export default async function ApplicationsPage() {
  if (!(await isSignedIn())) redirect("/login");

  const data = await api<{
    applications: ApplicationRow[];
    counters: { applications: number; saved: number; matches: number };
  }>("/api/v1/applications");

  return (
    <div>
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Applications</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
          {data.counters.applications} tracked · {data.counters.saved} saved ·{" "}
          {data.counters.matches} matches reviewed
        </p>
      </header>

      {data.applications.length === 0 ? (
        <p className="mt-6 text-sm text-[var(--color-ink-soft)]">
          Nothing tracked yet. When you apply to a role from your shortlist, it appears here — you
          send the application yourself, and record it so nothing gets applied to twice.
        </p>
      ) : (
        <ul className="mt-6 space-y-3">
          {data.applications.map((row) => (
            <li
              key={row.id}
              className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-medium">{row.title}</p>
                  <p className="text-sm text-[var(--color-ink-soft)]">{row.company}</p>
                  <p className="mt-1 text-xs text-[var(--color-ink-soft)]">
                    Applied {relative(row.applied_at)} ·{" "}
                    <Verification status={row.url_status} at={null} />
                  </p>
                </div>

                <form
                  action={`/api/applications/${row.id}/status`}
                  method="post"
                  className="flex items-center gap-2"
                >
                  <select
                    name="status"
                    defaultValue={row.status}
                    className="rounded-md border border-[var(--color-line)] bg-[var(--color-paper)] px-2 py-1 text-sm"
                  >
                    {STATUSES.map((status) => (
                      <option key={status} value={status}>
                        {status.replace(/_/g, " ")}
                      </option>
                    ))}
                  </select>
                  <button className="rounded-md border border-[var(--color-line)] px-3 py-1 text-sm">
                    Update
                  </button>
                </form>
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-8 text-xs text-[var(--color-ink-soft)]">
        &quot;No response&quot; is a status here because it is the most common outcome, and a
        tracker without a word for it quietly teaches people that silence is failure.
      </p>
    </div>
  );
}
