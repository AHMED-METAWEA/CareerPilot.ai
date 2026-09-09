/**
 * What is missing, stated plainly (§2.3).
 *
 * The product's third promise is honest gaps: naming what the candidate does
 * not have, without inventing anything to close it. That means this component
 * never suggests wording for a CV — it says what the posting asked for and the
 * CV did not evidence, and leaves the judgement to the person.
 */
export function GapReport({ gaps, matched }: { gaps: string[]; matched: string }) {
  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <h3 className="text-sm font-medium">Where you stand</h3>
      <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{matched}</p>

      {gaps.length === 0 ? (
        <p className="mt-3 text-sm">
          Nothing in the listed requirements is unevidenced by your CV.
        </p>
      ) : (
        <>
          <p className="mt-3 text-sm">The posting asks for these, and your CV does not show them:</p>
          <ul className="mt-2 space-y-1 text-sm">
            {gaps.map((gap) => (
              <li key={gap} className="flex gap-2">
                <span aria-hidden className="text-[var(--color-missing)]">
                  •
                </span>
                <span>{gap}</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-[var(--color-ink-soft)]">
            A gap is not always a reason not to apply. It is a thing to be ready to talk about —
            or, if you do have it, a thing missing from your CV rather than from you.
          </p>
        </>
      )}
    </section>
  );
}
