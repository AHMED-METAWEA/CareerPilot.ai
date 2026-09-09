import Link from "next/link";
import { redirect } from "next/navigation";
import { BulletWorkshop } from "@/components/BulletWorkshop";
import { CoverLetterPanel } from "@/components/CoverLetterPanel";
import {
  api,
  ApiError,
  isSignedIn,
  type GapAnalysis,
  type InterviewPrep,
} from "@/lib/api";
import { currentLocale, translator } from "@/lib/i18n";

/**
 * Preparation for one role (§18, Phase 4).
 *
 * Three things, in the order a candidate actually needs them: what is missing,
 * what they are likely to be asked, and — only if they want it — a draft letter.
 *
 * The first two need no model and are the same every time this page is opened.
 * The third is generated, and says so. Nothing here fills a gap by inventing
 * something to put in it: the suggestion for a missing skill is a thing to go
 * and do, not a phrase to paste into a CV.
 */
export default async function PreparePage({ params }: { params: Promise<{ id: string }> }) {
  if (!(await isSignedIn())) redirect("/login");
  const { id } = await params;

  const [gaps, prep, bullets, locale] = await Promise.all([
    api<GapAnalysis>(`/api/v1/matches/${id}/gaps`),
    api<InterviewPrep>(`/api/v1/matches/${id}/interview`).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }),
    api<{ bullets: string[] }>(`/api/v1/matches/${id}/bullets`).catch(() => ({ bullets: [] })),
    currentLocale(),
  ]);
  const t = translator(locale);

  return (
    <div className="space-y-6">
      <Link href={`/matches/${id}`} className="text-sm underline">
        <span aria-hidden className="inline-block rtl:rotate-180">
          ←
        </span>{" "}
        {t("prepare.back")}
      </Link>

      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Preparing for {prep?.role ?? "this role"}
        </h1>
        {prep?.company ? (
          <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{prep.company}</p>
        ) : null}
      </header>

      <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
        <h2 className="text-sm font-medium">{t("prepare.gaps")}</h2>
        {gaps.gaps.length === 0 ? (
          <p className="mt-2 text-sm">{t("prepare.noGaps")}</p>
        ) : (
          <ul className="mt-3 space-y-4">
            {gaps.gaps.map((gap) => (
              <li key={gap.skill} className="border-s-2 border-[var(--color-line)] ps-3">
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-medium">{gap.skill}</span>
                  {gap.must_have ? (
                    <span className="rounded bg-[var(--color-missing)]/10 px-1.5 py-0.5 text-[11px] text-[var(--color-missing)]">
                      {t("prepare.required")}
                    </span>
                  ) : (
                    <span className="text-[11px] text-[var(--color-ink-soft)]">
                      {t("prepare.niceToHave")}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{gap.suggestion}</p>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-4 text-xs text-[var(--color-ink-soft)]">{gaps.note}</p>
      </section>

      {prep ? (
        <section>
          <h2 className="text-lg font-medium">{t("prepare.questions")}</h2>
          <p className="mb-4 mt-1 text-sm text-[var(--color-ink-soft)]">{prep.note}</p>
          <ul className="space-y-3">
            {prep.questions.map((question) => (
              <li
                key={question.requirement}
                className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4"
              >
                <p className="text-sm font-medium">{question.likely_question}</p>
                <p className="mt-1 text-xs text-[var(--color-ink-soft)]">
                  {t("prepare.fromPosting")}: “{question.requirement}”
                  {question.must_have ? ` · ${t("prepare.stated")}` : null}
                </p>
                {question.your_evidence ? (
                  <blockquote className="mt-3 border-s-2 border-[var(--color-accent)] ps-3 text-sm">
                    {question.your_evidence}
                  </blockquote>
                ) : null}
                <p className="mt-2 text-xs text-[var(--color-ink-soft)]">{question.advice}</p>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <BulletWorkshop matchId={id} bullets={bullets.bullets} labels={bulletLabels(t)} />

      <CoverLetterPanel matchId={id} labels={letterLabels(t)} />
    </div>
  );
}

/* The two panels below are client components, so their copy is passed in
   already translated rather than making each of them fetch the locale. */
function bulletLabels(t: ReturnType<typeof translator>) {
  return {
    heading: t("prepare.bullets"),
    intro: t("prepare.bulletsIntro"),
    rewrite: t("prepare.rewrite"),
    rewriting: t("prepare.rewriting"),
    refused: t("prepare.refused"),
  };
}

function letterLabels(t: ReturnType<typeof translator>) {
  return {
    heading: t("prepare.letter"),
    intro: t("prepare.letterIntro"),
    draftOne: t("prepare.draftOne"),
    draftAgain: t("prepare.draftAgain"),
    drafting: t("prepare.drafting"),
    refused: t("prepare.refused"),
    refusedNote: t("prepare.refusedNote"),
  };
}
