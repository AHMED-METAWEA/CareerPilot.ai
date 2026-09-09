import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const form = await request.formData();

  await fetch(`${API_BASE}/api/v1/applications/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ status: String(form.get("status") ?? "") }),
  }).catch(() => undefined);

  return NextResponse.redirect(new URL("/applications", request.url), { status: 303 });
}
