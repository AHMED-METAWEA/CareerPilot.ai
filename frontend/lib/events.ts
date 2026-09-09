import type { NextRequest } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "./api";

/**
 * Record an engagement event (§11.6 step 4).
 *
 * Failures are swallowed on purpose: a save that does not register is a lost
 * signal, not a reason to show the candidate an error page in the middle of
 * reading a job.
 */
export async function recordEvent(
  request: NextRequest,
  postingId: string,
  event: "viewed" | "saved" | "dismissed" | "applied",
): Promise<void> {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  await fetch(`${API_BASE}/api/v1/jobs/${postingId}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ event }),
  }).catch(() => undefined);
}
