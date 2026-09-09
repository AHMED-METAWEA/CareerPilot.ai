import { redirect } from "next/navigation";
import { api, isSignedIn } from "@/lib/api";
import { currentLocale, translator, type Locale } from "@/lib/i18n";

type Consents = {
  active: string[];
  available: string[];
  history: {
    purpose: string;
    policy_version: string;
    granted_at: string;
    revoked_at: string | null;
  }[];
};

const PURPOSE_COPY: Record<string, { label: string; detail: string }> = {
  processing: {
    label: "Processing your CV",
    detail:
      "Required. Without it your CV cannot be handled at all. Withdrawing it means deleting your account.",
  },
  digest: {
    label: "Email digest",
    detail: "At most five new matches, with one line of reasoning each. Stoppable in one click.",
  },
  analytics: {
    label: "Product analytics",
    detail: "Aggregate usage only. Never shared, never used to train anything on your CV.",
  },
};

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ notice?: string }>;
}) {
  if (!(await isSignedIn())) redirect("/login");
  const { notice } = await searchParams;
  const [consents, locale] = await Promise.all([
    api<Consents>("/api/v1/me/consents"),
    currentLocale(),
  ]);
  const t = translator(locale);

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <h1 className="text-2xl font-semibold tracking-tight">{t("settings.title")}</h1>

      {notice ? (
        <p className="rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-sm">
          {notice}
        </p>
      ) : null}

      <section>
        <h2 className="text-sm font-medium">{t("settings.language")}</h2>
        <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{t("settings.languageDetail")}</p>
        <form action="/api/preferences" method="post" className="mt-3 flex items-center gap-3">
          <LanguageChoice current={locale} />
          <button className="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm">
            {t("settings.save")}
          </button>
        </form>
      </section>

      <section>
        <h2 className="text-sm font-medium">What you have agreed to</h2>
        <ul className="mt-3 space-y-3">
          {consents.available.map((purpose) => {
            const copy = PURPOSE_COPY[purpose];
            const active = consents.active.includes(purpose);
            return (
              <li
                key={purpose}
                className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4"
              >
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium">{copy?.label ?? purpose}</p>
                    <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{copy?.detail}</p>
                  </div>
                  {purpose === "processing" ? (
                    <span className="shrink-0 text-xs text-[var(--color-ink-soft)]">Required</span>
                  ) : (
                    <form action="/api/consents" method="post" className="shrink-0">
                      <input type="hidden" name="purpose" value={purpose} />
                      <input type="hidden" name="granted" value={active ? "false" : "true"} />
                      <button className="rounded-md border border-[var(--color-line)] px-3 py-1 text-sm">
                        {active ? "Turn off" : "Turn on"}
                      </button>
                    </form>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section>
        <h2 className="text-sm font-medium">Your data</h2>
        <div className="mt-3 space-y-3">
          <div className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4">
            <p className="text-sm font-medium">Export everything</p>
            <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
              A JSON file with your account, CV text, profile, matches and applications.
            </p>
            <a
              href="/api/export"
              className="mt-3 inline-block rounded-md border border-[var(--color-line)] px-3 py-1.5 text-sm"
            >
              Download
            </a>
          </div>

          <details className="rounded-lg border border-[var(--color-missing)] bg-[var(--color-surface)] p-4">
            <summary className="cursor-pointer text-sm font-medium">Delete your account</summary>
            <p className="mt-2 text-sm text-[var(--color-ink-soft)]">
              Your CV text and every session are destroyed immediately. The remaining records are
              deleted after 30 days — the window in which an accidental deletion can still be
              reversed. A record that the deletion happened is kept, and holds none of your data.
            </p>
            <form action="/api/account/delete" method="post" className="mt-3 flex gap-2">
              <input
                name="confirm_email"
                type="email"
                required
                placeholder="Type your email to confirm"
                className="flex-1 rounded-md border border-[var(--color-line)] bg-[var(--color-paper)] px-3 py-1.5 text-sm"
              />
              <button className="rounded-md border border-[var(--color-missing)] px-3 py-1.5 text-sm text-[var(--color-missing)]">
                Delete
              </button>
            </form>
          </details>
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium">Consent history</h2>
        <ul className="mt-3 space-y-1 text-xs text-[var(--color-ink-soft)]">
          {consents.history.map((row, index) => (
            <li key={`${row.purpose}-${index}`}>
              {row.purpose} · policy {row.policy_version} · granted{" "}
              {new Date(row.granted_at).toLocaleDateString()}
              {row.revoked_at
                ? ` · revoked ${new Date(row.revoked_at).toLocaleDateString()}`
                : ""}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/** Radio buttons rather than a select: two options, both worth reading. */
function LanguageChoice({ current }: { current: Locale }) {
  return (
    <span className="flex gap-4 text-sm">
      {(
        [
          ["en", "English"],
          ["ar", "العربية"],
        ] as const
      ).map(([value, label]) => (
        <label key={value} className="flex items-center gap-1.5">
          <input type="radio" name="locale" value={value} defaultChecked={current === value} />
          <span lang={value}>{label}</span>
        </label>
      ))}
    </span>
  );
}
