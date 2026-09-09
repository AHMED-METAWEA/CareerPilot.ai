import type { Metadata } from "next";
import Link from "next/link";
import { isSignedIn } from "@/lib/api";
import { currentLocale, isRtl, translator } from "@/lib/i18n";
import "./globals.css";

export const metadata: Metadata = {
  title: "CareerPilot.ai",
  description:
    "A small number of genuinely well-matched, verified live openings — with the reasoning shown.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const [signedIn, locale] = await Promise.all([isSignedIn(), currentLocale()]);
  const t = translator(locale);

  /* `dir` on <html> is what makes the whole tree mirror: Tailwind's logical
     properties (ms-, me-, ps-, pe-) and the browser's own text alignment both
     key off it, so no component needs an RTL branch of its own. */
  return (
    <html lang={locale} dir={isRtl(locale) ? "rtl" : "ltr"}>
      <body className="min-h-screen">
        <header className="border-b border-[var(--color-line)]">
          <nav className="mx-auto flex max-w-5xl items-center gap-6 px-5 py-4 text-sm">
            <Link href="/" className="font-semibold tracking-tight">
              CareerPilot<span className="text-[var(--color-accent)]">.ai</span>
            </Link>
            {signedIn ? (
              <>
                <Link href="/matches" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  {t("nav.matches")}
                </Link>
                <Link href="/applications" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  {t("nav.applications")}
                </Link>
                <Link href="/settings" className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                  {t("nav.settings")}
                </Link>
                <form action="/api/auth/logout" method="post" className="ms-auto">
                  <button className="text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                    {t("nav.signOut")}
                  </button>
                </form>
              </>
            ) : (
              <Link href="/login" className="ms-auto text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                {t("nav.signIn")}
              </Link>
            )}
          </nav>
        </header>

        <main className="mx-auto max-w-5xl px-5 py-8">{children}</main>

        <footer className="mx-auto max-w-5xl px-5 py-10 text-xs text-[var(--color-ink-soft)]">
          <p>{t("footer.promise")}</p>
        </footer>
      </body>
    </html>
  );
}
