import Link from "next/link";
import { redirect } from "next/navigation";
import { api, isSignedIn, type WithheldRow } from "@/lib/api";

/**
 * Why roles were held back (§8.3, §12.3).
 *
 * Withholding without an explanation is indistinguishable from a broken search,
 * and a candidate who cannot see the reason cannot fix the cause — a missing
 * work-authorisation declaration, or a location list that is too narrow.
 */
export default async function WithheldPage() {
  if (!(await isSignedIn())) redirect("/login");

  const data = await api<{ withheld: WithheldRow[]; summary: Record<string, number> }>(
    "/api/v1/matches/withheld",
  );
  const summary = Object.entries(data.summary).sort((a, b) => b[1] - a[1]);

  return (
    <div>
      <Link href="/matches" className="text-sm underline">
        ← Back to your shortlist
      </Link>
      <h1 className="mt-4 text-2xl font-semibold tracking-tight">Held back</h1>
      <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
        These roles were excluded before scoring. Most reasons are things you can change.
      </p>

      {summary.length > 0 ? (
        <ul className="mt-5 flex flex-wrap gap-2 text-xs">
          {summary.map(([code, count]) => (
            <li
              key={code}
              className="rounded-full border border-[var(--color-line)] px-3 py-1 text-[var(--color-ink-soft)]"
            >
              {code.replace(/_/g, " ")} · {count}
            </li>
          ))}
        </ul>
      ) : null}

      <ul className="mt-5 space-y-3">
        {data.withheld.map((row, index) => (
          <li
            key={`${row.title}-${index}`}
            className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4"
          >
            <p className="text-sm font-medium">{row.title}</p>
            <p className="text-sm text-[var(--color-ink-soft)]">{row.company}</p>
            <ul className="mt-2 space-y-1 text-sm text-[var(--color-ink-soft)]">
              {row.reasons.map((reason) => (
                <li key={reason.code}>{reason.explanation}</li>
              ))}
            </ul>
          </li>
        ))}
      </ul>

      {data.withheld.length === 0 ? (
        <p className="mt-6 text-sm text-[var(--color-ink-soft)]">
          Nothing has been held back yet.
        </p>
      ) : null}
    </div>
  );
}
