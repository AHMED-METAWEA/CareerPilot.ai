import type { ParseabilityReport as Report } from "@/lib/api";

/**
 * The CV parseability report (§11.1 step 5, §2.4).
 *
 * Real ATS failures are parsing failures, not keyword density. When a CV cannot
 * be read, the candidate gets the reason and the fix — not a score to optimise
 * and not a rejection.
 */
const SEVERITY: Record<string, string> = {
  blocker: "border-[var(--color-missing)]",
  warning: "border-[var(--color-partial)]",
  info: "border-[var(--color-line)]",
};

export function ParseabilityReport({ report }: { report: Report }) {
  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-medium">How well your CV reads to a parser</h3>
        <span className="text-sm text-[var(--color-ink-soft)]">
          {Math.round(report.quality * 100)}%
        </span>
      </div>

      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[var(--color-line)]">
        <div
          className={`h-full rounded-full ${
            report.is_processable ? "bg-[var(--color-met)]" : "bg-[var(--color-missing)]"
          }`}
          style={{ width: `${Math.round(report.quality * 100)}%` }}
        />
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-xs text-[var(--color-ink-soft)] sm:grid-cols-4">
        <Stat label="Sections found" value={`${report.sections_found.length}/4`} />
        <Stat label="Text per page" value={`${Math.round(report.character_yield_per_page)} chars`} />
        <Stat label="Column bleed" value={`${Math.round(report.layout_damage * 100)}%`} />
        <Stat label="Pages" value={String(report.page_count)} />
      </dl>

      {report.language === "ar" || report.is_mixed_script ? (
        <p className="mt-3 text-xs text-[var(--color-ink-soft)]">
          Read as{" "}
          {report.is_mixed_script
            ? "Arabic and English together"
            : report.language === "ar"
              ? "Arabic"
              : "English"}
          . Both scripts are matched in the same space, so a CV that names its
          technologies in English inside Arabic prose loses nothing.
        </p>
      ) : null}

      {report.findings.length > 0 ? (
        <ul className="mt-4 space-y-2">
          {report.findings.map((finding) => (
            <li
              key={finding.code}
              className={`rounded-md border-s-2 bg-[var(--color-paper)] p-3 text-sm ${
                SEVERITY[finding.severity] ?? SEVERITY.info
              }`}
            >
              <p>{finding.message}</p>
              <p className="mt-1 text-[var(--color-ink-soft)]">{finding.suggestion}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-4 text-sm text-[var(--color-ink-soft)]">
          Nothing to fix — this CV extracts cleanly.
        </p>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className="mt-0.5 text-sm text-[var(--color-ink)]">{value}</dd>
    </div>
  );
}
