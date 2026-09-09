import { NextRequest, NextResponse } from "next/server";
import { API_BASE } from "@/lib/api";
import { setSession } from "@/lib/session";

export async function POST(request: NextRequest) {
  const form = await request.formData();
  const response = await fetch(`${API_BASE}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: String(form.get("email") ?? ""),
      password: String(form.get("password") ?? ""),
      // Consent is explicit and recorded with its policy version (§11.1).
      accept_processing: form.get("accept_processing") === "on",
      accept_digest: form.get("accept_digest") === "on",
    }),
  });

  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const message =
      problem.problems?.join(" ") ?? problem.title ?? "That account could not be created";
    return NextResponse.redirect(
      new URL(`/register?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  const tokens = await response.json();
  const redirect = NextResponse.redirect(new URL("/onboarding", request.url), { status: 303 });
  setSession(redirect, tokens);
  return redirect;
}
