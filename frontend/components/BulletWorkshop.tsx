"use client";

import { useState } from "react";

type Result =
  | { kind: "idle" }
  | { kind: "working" }
  | { kind: "ready"; rewritten: string }
  | { kind: "refused"; invented: string[] }
  | { kind: "unavailable"; message: string };

/**
 * Rewriting the candidate's own bullets toward this posting (§18, Phase 4).
 *
 * Only bullets already extracted from their CV are offered. A free-text box
 * here would turn the endpoint into a way to launder a claim the CV never made,
 * and the guard would then be checking invented text against itself.
 *
 * The original stays on screen next to the rewrite. The candidate decides which
 * one is true to their work; nothing is written back to their CV.
 */
export function BulletWorkshop({ matchId, bullets }: { matchId: string; bullets: string[] }) {
  const [results, setResults] = useState<Record<number, Result>>({});

  async function rewrite(index: number, bullet: string) {
    setResults((current) => ({ ...current, [index]: { kind: "working" } }));
    const response = await fetch(`/api/matches/${matchId}/bullet`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bullet }),
    });
    const body = await response.json().catch(() => ({}));

    setResults((current) => ({
      ...current,
      [index]: response.ok
        ? { kind: "ready", rewritten: body.rewritten }
        : response.status === 422
          ? { kind: "refused", invented: body.invented ?? [] }
          : { kind: "unavailable", message: body.detail ?? "Rewriting is unavailable right now." },
    }));
  }

  if (bullets.length === 0) return null;

  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <h2 className="text-sm font-medium">Your bullets, aimed at this posting</h2>
      <p className="mt-1 text-xs text-[var(--color-ink-soft)]">
        Rephrasing only. A rewrite that adds a number, a tool or an employer your CV does not
        mention is discarded before you see it.
      </p>

      <ul className="mt-4 space-y-4">
        {bullets.map((bullet, index) => {
          const result = results[index] ?? { kind: "idle" };
          return (
            <li key={bullet} className="border-l-2 border-[var(--color-line)] pl-3">
              <p className="text-sm">{bullet}</p>
              <button
                onClick={() => rewrite(index, bullet)}
                disabled={result.kind === "working"}
                className="mt-2 text-xs underline disabled:opacity-50"
              >
                {result.kind === "working" ? "Rewriting…" : "Rewrite for this role"}
              </button>

              {result.kind === "ready" ? (
                <p className="mt-2 rounded-md border border-[var(--color-accent)]/40 bg-[var(--color-bg)] p-3 text-sm">
                  {result.rewritten}
                </p>
              ) : null}

              {result.kind === "refused" ? (
                <div className="mt-2 rounded-md border border-[var(--color-missing)]/40 p-3">
                  <p className="text-xs">
                    The rewrite added something your CV does not say, so it was discarded.
                  </p>
                  {result.invented.length > 0 ? (
                    <ul className="mt-1 text-xs text-[var(--color-ink-soft)]">
                      {result.invented.map((item) => (
                        <li key={item}>— {item}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}

              {result.kind === "unavailable" ? (
                <p className="mt-2 text-xs text-[var(--color-ink-soft)]">{result.message}</p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
