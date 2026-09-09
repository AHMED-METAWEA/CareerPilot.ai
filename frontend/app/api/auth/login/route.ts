import { NextRequest, NextResponse } from "next/server";
import { API_BASE } from "@/lib/api";
import { setSession } from "@/lib/session";

/**
 * Exchange credentials for tokens, and keep the tokens out of the browser.
 *
 * The API returns them in the body; they are stored in httpOnly cookies here
 * and never handed to client JavaScript, so an XSS bug cannot take a session
 * with it (§16.2).
 */
export async function POST(request: NextRequest) {
  const form = await request.formData();
  const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: String(form.get("email") ?? ""),
      password: String(form.get("password") ?? ""),
    }),
  });

  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const message = problem.title ?? "Email or password is incorrect";
    return NextResponse.redirect(
      new URL(`/login?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  const tokens = await response.json();
  const redirect = NextResponse.redirect(new URL("/matches", request.url), { status: 303 });
  setSession(redirect, tokens);
  return redirect;
}
