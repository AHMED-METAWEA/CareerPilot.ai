/**
 * Session cookies.
 *
 * Lives here rather than in a route file because a Next.js route module may
 * only export route handlers — and because both the login and register handlers
 * need it.
 */

import type { NextResponse } from "next/server";
import { ACCESS_COOKIE, REFRESH_COOKIE } from "./api";

export type Tokens = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
};

/**
 * Store tokens where client JavaScript cannot read them.
 *
 * `httpOnly` is the point: an XSS bug on this origin should not be able to walk
 * off with a session (§16.2). `sameSite: lax` keeps the cookie off
 * cross-site POSTs while still surviving a normal top-level navigation.
 */
export function setSession(response: NextResponse, tokens: Tokens): void {
  const secure = process.env.NODE_ENV === "production";

  response.cookies.set(ACCESS_COOKIE, tokens.access_token, {
    httpOnly: true,
    sameSite: "lax",
    secure,
    path: "/",
    maxAge: tokens.expires_in,
  });
  response.cookies.set(REFRESH_COOKIE, tokens.refresh_token, {
    httpOnly: true,
    sameSite: "lax",
    secure,
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
}
