import type { Metadata } from "next";
import Link from "next/link";
import { isSignedIn } from "@/lib/api";
import "./globals.css";

export const metadata: Metadata = {
  title: "CareerPilot.ai",
  description:
    "A small number of genuinely well-matched, verified live openings — with the reasoning shown.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const signedIn = await isSignedIn();

  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-[var(--color-line)]">
          <nav className="mx-auto flex max-w-5xl items-center gap-6 px-5 py-4 text-sm">
            <Link href="/" className="font-semibold tracking-tight">
              CareerPilot<span className="text-[var(--color-accent)]">.ai</span>
            </Link>
            {signedIn ? (
              <>
                <Link href="/matches" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  Matches
                </Link>
                <Link href="/applications" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  Applications
                </Link>
                <Link href="/settings" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  Settings
                </Link>
                <form action="/api/auth/logout" method="post" className="ml-auto">
                  <button className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                    Sign out
                  </button>
                </form>
              </>
            ) : (
              <Link href="/login" className="ml-auto text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                Sign in
              </Link>
            )}
          </nav>
        </header>

        <main className="mx-auto max-w-5xl px-5 py-8">{children}</main>

        <footer className="mx-auto max-w-5xl px-5 py-10 text-xs text-[var(--color-ink-soft)]">
          <p>
            CareerPilot never submits applications, never contacts employers on your behalf, and
            never adds a skill to your CV that you do not have.
          </p>
        </footer>
      </body>
    </html>
  );
}
