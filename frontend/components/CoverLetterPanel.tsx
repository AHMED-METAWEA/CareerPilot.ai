"use client";

import { useState } from "react";

type State =
  | { kind: "idle" }
  | { kind: "working" }
  | { kind: "ready"; letter: string; disclosure: string }
  | { kind: "refused"; invented: string[] }
  | { kind: "unavailable"; message: string };

type Labels = {
  heading: string;
  intro: string;
  draftOne: string;
  draftAgain: string;
  drafting: string;
  refused: string;
  refusedNote: string;
};

/**
 * The draft letter (§10.4), and what happens when it cannot be trusted.
 *
 * A refusal is shown as a refusal — with the specific claims that failed the
 * check — rather than as a draft with a warning above it. If the guard fires,
 * the candidate sees nothing to copy, because a draft on screen is a draft
 * that gets sent.
 */
export function CoverLetterPanel({ matchId, labels }: { matchId: string; labels: Labels }) {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function generate() {
    setState({ kind: "working" });
    const response = await fetch(`/api/matches/${matchId}/cover-letter`, { method: "POST" });
    const body = await response.json().catch(() => ({}));

    if (response.ok) {
      setState({ kind: "ready", letter: body.letter, disclosure: body.disclosure });
    } else if (response.status === 422) {
      setState({ kind: "refused", invented: body.invented ?? [] });
    } else {
      setState({
        kind: "unavailable",
        message:
          body.detail ??
          "Drafting is unavailable right now. The gaps and interview notes above do not need it.",
      });
    }
  }

  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium">{labels.heading}</h2>
          <p className="mt-1 text-xs text-[var(--color-ink-soft)]">{labels.intro}</p>
        </div>
        <button
          onClick={generate}
          disabled={state.kind === "working"}
          className="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm disabled:opacity-50"
        >
          {state.kind === "working"
            ? labels.drafting
            : state.kind === "ready"
              ? labels.draftAgain
              : labels.draftOne}
        </button>
      </div>

      {state.kind === "ready" ? (
        <>
          <p className="mt-4 whitespace-pre-line rounded-md border border-[var(--color-line)] bg-[var(--color-bg)] p-4 text-sm">
            {state.letter}
          </p>
          <p className="mt-3 text-xs text-[var(--color-ink-soft)]">{state.disclosure}</p>
        </>
      ) : null}

      {state.kind === "refused" ? (
        <div className="mt-4 rounded-md border border-[var(--color-missing)]/40 p-4">
          <p className="text-sm">{labels.refused}</p>
          {state.invented.length > 0 ? (
            <ul className="mt-2 space-y-1 text-xs text-[var(--color-ink-soft)]">
              {state.invented.map((item) => (
                <li key={item}>— {item}</li>
              ))}
            </ul>
          ) : null}
          <p className="mt-2 text-xs text-[var(--color-ink-soft)]">{labels.refusedNote}</p>
        </div>
      ) : null}

      {state.kind === "unavailable" ? (
        <p className="mt-4 text-sm text-[var(--color-ink-soft)]">{state.message}</p>
      ) : null}
    </section>
  );
}
