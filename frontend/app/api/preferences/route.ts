import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

/**
 * Persist the interface language on the account (§18, Phase 5).
 *
 * A form post rather than a fetch, so the switch works before any JavaScript
 * loads — the language control is the one setting a reader who cannot read the
 * current language most needs to reach.
 */
export async function POST(request: NextRequest) {
  const form = await request.formData();
  const locale = form.get("locale") === "ar" ? "ar" : "en";
  const token = request.cookies.get(ACCESS_COOKIE)?.value;

  const response = await fetch(`${API_BASE}/api/v1/me`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ locale }),
  });

  const notice = response.ok
    ? locale === "ar"
      ? "تم تحديث اللغة."
      : "Language updated."
    : "The language could not be saved.";

  return NextResponse.redirect(
    new URL(`/settings?notice=${encodeURIComponent(notice)}`, request.url),
    { status: 303 },
  );
}
