import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

/** Streams the export through, so the access token stays on the server. */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const response = await fetch(`${API_BASE}/api/v1/me/export`, {
    headers: { Authorization: `Bearer ${token ?? ""}` },
  });

  if (!response.ok) {
    return NextResponse.redirect(
      new URL("/settings?notice=Export%20failed", request.url),
      { status: 303 },
    );
  }

  return new NextResponse(await response.text(), {
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition": 'attachment; filename="careerpilot-export.json"',
    },
  });
}
