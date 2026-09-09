import Link from "next/link";
import { redirect } from "next/navigation";
import { ParseabilityReport } from "@/components/ParseabilityReport";
import { api, ApiError, isSignedIn, type Profile, type ParseabilityReport as Report } from "@/lib/api";

export default async function OnboardingPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; report?: string; done?: string }>;
}) {
  if (!(await isSignedIn())) redirect("/login");
  const { error, report, done } = await searchParams;

  let profile: Profile | null = null;
  try {
    profile = await api<Profile>("/api/v1/profile");
  } catch (caught) {
    if (!(caught instanceof ApiError) || caught.status !== 404) throw caught;
  }

  const parsed: Report | null = report ? (JSON.parse(report) as Report) : null;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Your CV</h1>
        <p className="mt-2 text-sm text-[var(--color-ink-soft)]">
          PDF or DOCX, up to 10 MB. Your name, email, phone and address are removed before any
          text is sent to a language model, and every extracted fact is checked against the words
          actually in your document.
        </p>
      </header>

      {error ? (
        <p role="alert" className="rounded-md border border-[var(--color-missing)] bg-[var(--color-surface)] p-3 text-sm">
          {error}
        </p>
      ) : null}

      {done ? (
        <p className="rounded-md border border-[var(--color-met)] bg-[var(--color-surface)] p-3 text-sm">
          Profile extracted. <Link href="/matches" className="underline">See your shortlist</Link>.
        </p>
      ) : null}

      <form
        action="/api/cv/upload"
        method="post"
        encType="multipart/form-data"
        className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
      >
        <label className="block text-sm">
          <span className="text-[var(--color-ink-soft)]">Choose a file</span>
          <input
            type="file"
            name="file"
            required
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            className="mt-2 block w-full text-sm"
          />
        </label>
        <button className="mt-4 rounded-md bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white">
          Upload and extract
        </button>
      </form>

      {parsed ? (
        <>
          <p className="text-sm">
            This CV could not be read reliably enough to extract from. Nothing was guessed — here
            is what went wrong:
          </p>
          <ParseabilityReport report={parsed} />
        </>
      ) : null}

      {profile ? <ProfileSummary profile={profile} /> : null}
    </div>
  );
}

function ProfileSummary({ profile }: { profile: Profile }) {
  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium">What we read from your CV</h2>
        {profile.extraction_confidence !== null ? (
          <span className="text-xs text-[var(--color-ink-soft)]">
            {Math.round(profile.extraction_confidence * 100)}% of extracted fields verified
          </span>
        ) : null}
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-4 text-sm">
        <div>
          <dt className="text-xs text-[var(--color-ink-soft)]">Experience</dt>
          <dd>{profile.years_experience ? `${profile.years_experience} years` : "Not stated"}</dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--color-ink-soft)]">Level</dt>
          <dd>{profile.seniority_level ?? "Not stated"}</dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--color-ink-soft)]">Locations</dt>
          <dd>{profile.locations.join(", ") || "Not stated"}</dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--color-ink-soft)]">Languages</dt>
          <dd>
            {profile.languages.map((l) => `${l.lang}${l.cefr ? ` (${l.cefr})` : ""}`).join(", ") ||
              "Not stated"}
          </dd>
        </div>
      </dl>

      <h3 className="mt-5 text-xs text-[var(--color-ink-soft)]">
        Skills ({profile.skills.length}) — each one appears in your CV
      </h3>
      <ul className="mt-2 flex flex-wrap gap-2">
        {profile.skills.map((skill) => (
          <li
            key={skill.name}
            title={`Evidenced at characters ${skill.evidence_span[0]}–${skill.evidence_span[1]} of your CV`}
            className="rounded-full border border-[var(--color-line)] px-3 py-1 text-xs"
          >
            {skill.name}
          </li>
        ))}
      </ul>

      <p className="mt-4 text-xs text-[var(--color-ink-soft)]">
        Anything the extraction could not point to in your document was discarded rather than
        shown. If something is missing here, it is usually missing from the CV.
      </p>
    </section>
  );
}
