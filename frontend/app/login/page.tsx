import Link from "next/link";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <div className="mx-auto max-w-sm py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>

      {error ? (
        <p
          role="alert"
          className="mt-4 rounded-md border border-[var(--color-missing)] bg-[var(--color-surface)] p-3 text-sm"
        >
          {error}
        </p>
      ) : null}

      <form action="/api/auth/login" method="post" className="mt-6 space-y-4">
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
            autoComplete="current-password"
            className="mt-1 w-full rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2"
          />
        </label>
        <button className="w-full rounded-md bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white">
          Sign in
        </button>
      </form>

      <p className="mt-6 text-sm text-[var(--color-ink-soft)]">
        No account yet? <Link href="/register" className="underline">Create one</Link>.
      </p>
    </div>
  );
}
