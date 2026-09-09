import { cookies } from "next/headers";
import { ACCESS_COOKIE, API_BASE } from "./api";

/**
 * Interface language (§18, Phase 5).
 *
 * Two locales, one dictionary, no library. A translation framework would buy
 * plural rules and message formatting this interface does not use, and would
 * cost a build step and a runtime bundle on every page.
 *
 * The locale lives on the user's account, not in a cookie, so it follows them
 * between devices. Signed-out pages read English, because the sign-in page is
 * the one place we cannot know who is reading.
 *
 * **Untranslated strings are visible, not hidden.** A missing key falls back to
 * English rather than to the key name or an empty string: a candidate reading a
 * mostly-Arabic page with one English sentence in it can still act on that
 * sentence. See `docs/EVALUATION.md` for what is translated so far.
 */

export type Locale = "en" | "ar";

export const LOCALES: Locale[] = ["en", "ar"];

export function isRtl(locale: Locale): boolean {
  return locale === "ar";
}

export async function currentLocale(): Promise<Locale> {
  const store = await cookies();
  const token = store.get(ACCESS_COOKIE)?.value;
  if (!token) return "en";

  try {
    const response = await fetch(`${API_BASE}/api/v1/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) return "en";
    const body = await response.json();
    return body.locale === "ar" ? "ar" : "en";
  } catch {
    // The language of the interface is not worth an error page.
    return "en";
  }
}

const en = {
  // Chrome
  "nav.matches": "Matches",
  "nav.applications": "Applications",
  "nav.settings": "Settings",
  "nav.signIn": "Sign in",
  "nav.signOut": "Sign out",
  "footer.promise":
    "CareerPilot never submits applications, never contacts employers on your behalf, and never adds a skill to your CV that you do not have.",

  // Shortlist
  "matches.title": "Your shortlist",
  "matches.empty": "No matches yet. Upload a CV to get started.",
  "matches.refresh": "Refresh matches",
  "matches.withheld": "See what was withheld, and why",
  "matches.poolSize": "out of {count} openings considered",

  // Match detail
  "match.back": "Back to your shortlist",
  "match.apply": "Apply on the employer's site",
  "match.save": "Save",
  "match.dismiss": "Not for me",
  "match.prepare": "Prepare",
  "match.requirements": "Requirement by requirement",
  "match.requirementsIntro":
    "Each line below is a requirement from the posting, with the sentence from your CV that answered it.",
  "match.scoring": "How this was scored",
  "match.scoringIntro":
    "Five named terms, combined by fixed weights. No model produces any of these numbers.",
  "match.fromPosting": "From the posting",
  "match.excerptNote": "An excerpt only. Read the full description on the employer's page.",

  // Preparation
  "prepare.back": "Back to the evidence",
  "prepare.gaps": "What to close, and how",
  "prepare.noGaps": "Nothing in the listed requirements is unevidenced by your CV.",
  "prepare.required": "required",
  "prepare.niceToHave": "nice to have",
  "prepare.questions": "What you are likely to be asked",
  "prepare.fromPosting": "From the posting",
  "prepare.stated": "stated as required",
  "prepare.bullets": "Your bullets, aimed at this posting",
  "prepare.bulletsIntro":
    "Rephrasing only. A rewrite that adds a number, a tool or an employer your CV does not mention is discarded before you see it.",
  "prepare.rewrite": "Rewrite for this role",
  "prepare.rewriting": "Rewriting…",
  "prepare.letter": "A draft cover letter",
  "prepare.letterIntro":
    "Written from your CV and this posting only. Every claim in it is checked against your CV before you see it.",
  "prepare.draftOne": "Draft one",
  "prepare.draftAgain": "Draft again",
  "prepare.drafting": "Drafting…",
  "prepare.refused":
    "The draft claimed things your CV does not support, so it was discarded rather than shown to you.",
  "prepare.refusedNote":
    "This is the guard working, not a mistake you made. Try again, or write it yourself from the evidence on the previous page.",

  // Settings
  "settings.title": "Settings",
  "settings.language": "Interface language",
  "settings.languageDetail":
    "Applies everywhere you sign in. Matching works the same in either language — your CV and the postings are read in the language they are written in.",
  "settings.save": "Save",
  "settings.saved": "Language updated.",
} as const;

export type MessageKey = keyof typeof en;

const ar: Partial<Record<MessageKey, string>> = {
  "nav.matches": "الوظائف المرشحة",
  "nav.applications": "طلباتي",
  "nav.settings": "الإعدادات",
  "nav.signIn": "تسجيل الدخول",
  "nav.signOut": "تسجيل الخروج",
  "footer.promise":
    "لا يقدّم CareerPilot طلبات التوظيف نيابةً عنك، ولا يتواصل مع أصحاب العمل باسمك، ولا يضيف إلى سيرتك الذاتية مهارةً لا تمتلكها.",

  "matches.title": "قائمتك المختصرة",
  "matches.empty": "لا توجد نتائج بعد. ارفع سيرتك الذاتية للبدء.",
  "matches.refresh": "تحديث النتائج",
  "matches.withheld": "اعرض ما تم استبعاده، ولماذا",
  "matches.poolSize": "من بين {count} وظيفة تم فحصها",

  "match.back": "العودة إلى قائمتك",
  "match.apply": "التقديم على موقع جهة العمل",
  "match.save": "حفظ",
  "match.dismiss": "غير مناسبة لي",
  "match.prepare": "الاستعداد",
  "match.requirements": "متطلب بمتطلب",
  "match.requirementsIntro":
    "كل سطر أدناه متطلب من الإعلان، ومعه الجملة من سيرتك الذاتية التي تجيب عليه.",
  "match.scoring": "كيف تم التقييم",
  "match.scoringIntro":
    "خمسة عناصر محددة، تُجمع بأوزان ثابتة. لا ينتج أي نموذج ذكاء اصطناعي أيًّا من هذه الأرقام.",
  "match.fromPosting": "من الإعلان",
  "match.excerptNote": "مقتطف فقط. اقرأ الوصف الكامل على صفحة جهة العمل.",

  "prepare.back": "العودة إلى الأدلة",
  "prepare.gaps": "ما ينقصك، وكيف تسدّه",
  "prepare.noGaps": "لا يوجد متطلب من المتطلبات المذكورة دون دليل في سيرتك الذاتية.",
  "prepare.required": "مطلوب",
  "prepare.niceToHave": "يُفضّل توفره",
  "prepare.questions": "ما يُرجَّح أن تُسأل عنه",
  "prepare.fromPosting": "من الإعلان",
  "prepare.stated": "مذكور كمتطلب أساسي",
  "prepare.bullets": "نقاط سيرتك الذاتية، موجّهة لهذا الإعلان",
  "prepare.bulletsIntro":
    "إعادة صياغة فقط. أي إعادة صياغة تضيف رقمًا أو أداةً أو جهة عمل لا تذكرها سيرتك الذاتية تُرفض قبل أن تصل إليك.",
  "prepare.rewrite": "أعد الصياغة لهذه الوظيفة",
  "prepare.rewriting": "جارٍ إعادة الصياغة…",
  "prepare.letter": "مسودة خطاب تغطية",
  "prepare.letterIntro":
    "مكتوبة من سيرتك الذاتية ومن هذا الإعلان فقط. كل ادعاء فيها يُراجَع مقابل سيرتك الذاتية قبل أن تراه.",
  "prepare.draftOne": "اكتب مسودة",
  "prepare.draftAgain": "اكتب مسودة أخرى",
  "prepare.drafting": "جارٍ الكتابة…",
  "prepare.refused":
    "ادّعت المسودة أمورًا لا تدعمها سيرتك الذاتية، فتم رفضها بدلًا من عرضها عليك.",
  "prepare.refusedNote":
    "هذا يعني أن الحماية تعمل، وليس أنك أخطأت. جرّب مرة أخرى، أو اكتبه بنفسك من الأدلة في الصفحة السابقة.",

  "settings.title": "الإعدادات",
  "settings.language": "لغة الواجهة",
  "settings.languageDetail":
    "تُطبَّق أينما سجّلت الدخول. المطابقة تعمل بالطريقة نفسها في اللغتين — تُقرأ سيرتك الذاتية والإعلانات باللغة المكتوبة بها.",
  "settings.save": "حفظ",
  "settings.saved": "تم تحديث اللغة.",
};

const DICTIONARIES: Record<Locale, Partial<Record<MessageKey, string>>> = { en, ar };

/**
 * A translator bound to one locale.
 *
 * `{name}` placeholders are replaced from `values`. Anything else is returned
 * as written — this is a lookup, not a template language.
 */
export function translator(locale: Locale) {
  return function t(key: MessageKey, values?: Record<string, string | number>): string {
    const message = DICTIONARIES[locale][key] ?? en[key];
    if (!values) return message;
    return message.replace(/\{(\w+)\}/g, (whole, name: string) =>
      name in values ? String(values[name]) : whole,
    );
  };
}
