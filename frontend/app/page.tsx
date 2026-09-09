import Link from "next/link";
import { redirect } from "next/navigation";
import { isSignedIn } from "@/lib/api";

export default async function Home() {
  if (await isSignedIn()) redirect("/matches");

  return (
    <div className="mx-auto max-w-2xl py-10">
      <h1 className="text-3xl font-semibold tracking-tight">
        A short list of roles worth your time — and the reasoning behind each one.
      </h1>
      <p className="mt-5 text-[var(--color-ink-soft)]">
        CareerPilot reads employer applicant-tracking systems directly, checks that every opening
        is still live before showing it, and explains each match requirement by requirement,
        citing the words in your own CV that justified it.
      </p>

      <ul className="mt-8 space-y-3 text-sm">
        {[
          ["Verified openings", "Every role is checked live before it appears. No dead links."],
          [
            "Explained fit",
            "Requirement by requirement, with the line from your CV that answered it.",
          ],
          ["Honest gaps", "What is missing is named. Nothing is invented to close it."],
          [
            "You apply",
            "CareerPilot never submits an application or contacts anyone on your behalf.",
          ],
        ].map(([title, detail]) => (
          <li key={title} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4">
            <p className="font-medium">{title}</p>
            <p className="mt-1 text-[var(--color-ink-soft)]">{detail}</p>
          </li>
        ))}
      </ul>

      <div className="mt-8 flex gap-3">
        <Link
          href="/register"
          className="rounded-md bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white"
        >
          Create an account
        </Link>
        <Link
          href="/login"
          className="rounded-md border border-[var(--color-line)] px-4 py-2 text-sm font-medium"
        >
          Sign in
        </Link>
      </div>
    </div>
  );
}
