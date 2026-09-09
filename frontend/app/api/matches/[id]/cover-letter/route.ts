import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

/**
 * Proxy the draft request so the access token stays in the httpOnly cookie.
 *
 * The refusal is forwarded intact. An RFC 7807 problem document from this API
 * carries the human sentence in `title` and the failed claims in `invented`
 * (see `app/api/errors.py`), so the panel can show the candidate exactly what
 * was rejected — the difference between "something went wrong" and "here is
 * why you are not being shown a letter".
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const token = request.cookies.get(ACCESS_COOKIE)?.value;

  const response = await fetch(`${API_BASE}/api/v1/matches/${id}/cover-letter`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token ?? ""}` },
  });

  const body = await response.json().catch(() => ({}));
  if (response.ok) return NextResponse.json(body, { status: response.status });

  return NextResponse.json(
    { detail: body?.title ?? "Drafting failed.", invented: body?.invented ?? [] },
    { status: response.status },
  );
}
