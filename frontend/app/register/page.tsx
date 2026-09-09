import Link from "next/link";

export default async function RegisterPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <div className="mx-auto max-w-sm py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Create an account</h1>

      {error ? (
        <p
          role="alert"
          className="mt-4 rounded-md border border-[var(--color-missing)] bg-[var(--color-surface)] p-3 text-sm"
        >
          {error}
        </p>
      ) : null}

      <form action="/api/auth/register" method="post" className="mt-6 space-y-4">
        <label className="block text-sm">
          <span className="text-[var(--color-ink-soft)]">Email</span>
          <input
            name="email"
            type="email"
            required
            autoComplete="email"
            className="mt-1 w-full rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2"
          />
        </label>

        <label className="block text-sm">
          <span className="text-[var(--color-ink-soft)]">Password</span>
          <input
            name="password"
            type="password"
            required
            minLength={12}
            autoComplete="new-password"
            className="mt-1 w-full rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2"
          />
          <span className="mt-1 block text-xs text-[var(--color-ink-soft)]">
            At least 12 characters. A short phrase works better than a tortured word.
          </span>
        </label>

        {/* Consent is explicit, and recorded with the policy version it was
            given against (§11.1, §16.3). */}
        <label className="flex gap-2 text-sm">
          <input name="accept_processing" type="checkbox" required className="mt-1" />
          <span>
            I agree to CareerPilot processing my CV to find matching roles. Personal details are
            removed before any text is sent to a language model, and I can export or delete
            everything at any time.
          </span>
        </label>

        <label className="flex gap-2 text-sm">
          <input name="accept_digest" type="checkbox" className="mt-1" />
          <span className="text-[var(--color-ink-soft)]">
            Email me a short digest of new matches. Optional, and stoppable in one click.
          </span>
        </label>

        <button className="w-full rounded-md bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white">
          Create account
        </button>
      </form>

      <p className="mt-6 text-sm text-[var(--color-ink-soft)]">
        Already registered? <Link href="/login" className="underline">Sign in</Link>.
      </p>
    </div>
  );
}
