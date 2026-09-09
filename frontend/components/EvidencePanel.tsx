import type { RequirementEvidence } from "@/lib/api";

/**
 * Requirement-by-requirement evidence (§11.6 step 3).
 *
 * The distinctive claim of the product is here: each requirement shows the line
 * from the candidate's own CV that answered it. A requirement with no evidence
 * says so plainly rather than showing a weak match dressed up as a strong one —
 * the scorer already suppressed anything below its threshold, and repeating
 * that suppression visually is what keeps the two consistent.
 */

const STATUS_STYLE: Record<RequirementEvidence["status"], { label: string; className: string }> = {
  met: { label: "Met", className: "text-[var(--color-met)] border-[var(--color-met)]" },
  partial: {
    label: "Partial",
    className: "text-[var(--color-partial)] border-[var(--color-partial)]",
  },
  missing: {
    label: "Not evidenced",
    className: "text-[var(--color-missing)] border-[var(--color-missing)]",
  },
};

export function EvidencePanel({ requirements }: { requirements: RequirementEvidence[] }) {
  if (requirements.length === 0) {
    return (
      <p className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4 text-sm text-[var(--color-ink-soft)]">
        No requirements could be extracted from this posting, so this match was scored only on the
        parts we could assess. Its score is capped accordingly.
      </p>
    );
  }

  const mustHaves = requirements.filter((item) => item.must_have);
  const niceToHaves = requirements.filter((item) => !item.must_have);

  return (
    <div className="space-y-6">
      {mustHaves.length > 0 && <Group title="Must-haves" items={mustHaves} />}
      {niceToHaves.length > 0 && <Group title="Nice to have" items={niceToHaves} />}
    </div>
  );
}

function Group({ title, items }: { title: string; items: RequirementEvidence[] }) {
  const met = items.filter((item) => item.status === "met").length;

  return (
    <section>
      <h3 className="flex items-baseline gap-2 text-sm font-medium">
        {title}
        <span className="text-[var(--color-ink-soft)]">
          {met}/{items.length} evidenced
        </span>
      </h3>

      <ul className="mt-3 space-y-3">
        {items.map((item, index) => {
          const style = STATUS_STYLE[item.status];
          return (
            <li
              key={`${item.requirement}-${index}`}
              className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <p className="text-sm">{item.requirement}</p>
                <span
                  className={`shrink-0 rounded-full border px-2 py-0.5 text-xs ${style.className}`}
                >
                  {style.label}
                </span>
              </div>

              {item.evidence_from_cv ? (
                <blockquote className="mt-3 border-s-2 border-[var(--color-accent)] ps-3 text-sm text-[var(--color-ink-soft)]">
                  <span className="block text-xs uppercase tracking-wide">From your CV</span>
                  {item.evidence_from_cv}
                </blockquote>
              ) : (
                <p className="mt-3 text-sm text-[var(--color-ink-soft)]">
                  Nothing in your CV clearly answers this. That may be a gap, or it may be
                  something you have done and not written down.
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
