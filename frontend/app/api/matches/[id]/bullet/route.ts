import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

/** Proxy a rewrite request, keeping the access token in the httpOnly cookie. */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const payload = await request.json().catch(() => ({}));

  const response = await fetch(`${API_BASE}/api/v1/matches/${id}/bullet`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ bullet: payload?.bullet ?? "" }),
  });

  const body = await response.json().catch(() => ({}));
  if (response.ok) return NextResponse.json(body, { status: response.status });

  return NextResponse.json(
    { detail: body?.title ?? "The rewrite failed.", invented: body?.invented ?? [] },
    { status: response.status },
  );
}
