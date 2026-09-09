import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE, REFRESH_COOKIE } from "@/lib/api";

export async function POST(request: NextRequest) {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const form = await request.formData();

  const response = await fetch(`${API_BASE}/api/v1/me`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ confirm_email: String(form.get("confirm_email") ?? "") }),
  });

  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    return NextResponse.redirect(
      new URL(
        `/settings?notice=${encodeURIComponent(problem.title ?? "Deletion could not be confirmed")}`,
        request.url,
      ),
      { status: 303 },
    );
  }

  const redirect = NextResponse.redirect(new URL("/", request.url), { status: 303 });
  redirect.cookies.delete(ACCESS_COOKIE);
  redirect.cookies.delete(REFRESH_COOKIE);
  return redirect;
}
