import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

export async function POST(request: NextRequest) {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const form = await request.formData();

  const response = await fetch(`${API_BASE}/api/v1/me/consents`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({
      purpose: String(form.get("purpose") ?? ""),
      granted: form.get("granted") === "true",
    }),
  });

  const notice = response.ok
    ? "Preference saved."
    : ((await response.json().catch(() => ({}))).title ?? "Could not save that preference.");

  return NextResponse.redirect(
    new URL(`/settings?notice=${encodeURIComponent(notice)}`, request.url),
    { status: 303 },
  );
}
