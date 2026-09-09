import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

export async function POST(request: NextRequest) {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const response = await fetch(`${API_BASE}/api/v1/matches/refresh`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token ?? ""}` },
  });

  // 409 means a run is already queued — not an error worth showing.
  const message = response.ok
    ? "Matching queued. New results appear here when the run finishes."
    : response.status === 409
      ? "A matching run is already in progress."
      : "Could not queue a run just now.";

  return NextResponse.redirect(
    new URL(`/matches?notice=${encodeURIComponent(message)}`, request.url),
    { status: 303 },
  );
}
