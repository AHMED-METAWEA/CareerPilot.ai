import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE, REFRESH_COOKIE } from "@/lib/api";

export async function POST(request: NextRequest) {
  const access = request.cookies.get(ACCESS_COOKIE)?.value;
  const refresh = request.cookies.get(REFRESH_COOKIE)?.value;

  // Revoke server-side too: clearing a cookie ends the browser's session, not
  // the refresh token's (§16.2).
  if (access && refresh) {
    await fetch(`${API_BASE}/api/v1/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${access}` },
      body: JSON.stringify({ refresh_token: refresh }),
    }).catch(() => undefined);
  }

  const response = NextResponse.redirect(new URL("/login", request.url), { status: 303 });
  response.cookies.delete(ACCESS_COOKIE);
  response.cookies.delete(REFRESH_COOKIE);
  return response;
}
