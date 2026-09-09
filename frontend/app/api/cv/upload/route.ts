import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, API_BASE } from "@/lib/api";

/**
 * Upload a CV, then immediately extract a profile from it.
 *
 * A CV that cannot be read comes back as a 422 carrying the parseability
 * report; that report is what the candidate needs, so it is passed through to
 * the onboarding page rather than flattened into "upload failed".
 */
export async function POST(request: NextRequest) {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const incoming = await request.formData();
  const file = incoming.get("file");

  if (!(file instanceof File) || file.size === 0) {
    return redirectWith(request, { error: "Choose a PDF or DOCX file to upload." });
  }

  const body = new FormData();
  body.append("file", file);

  const upload = await fetch(`${API_BASE}/api/v1/cv`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token ?? ""}` },
    body,
  });
  const result = await upload.json().catch(() => ({}));

  if (!upload.ok) {
    if (result?.parseability) {
      return redirectWith(request, { report: JSON.stringify(result.parseability) });
    }
    return redirectWith(request, { error: result?.title ?? "That file could not be read." });
  }

  const profile = await fetch(`${API_BASE}/api/v1/cv/${result.cv_version_id}/profile`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token ?? ""}` },
  });

  if (!profile.ok) {
    const problem = await profile.json().catch(() => ({}));
    return redirectWith(request, {
      report: JSON.stringify(result.parseability),
      // A missing inference key is a deployment fact, not the user's problem.
      error: problem?.title ?? "Your CV was stored, but the profile could not be extracted yet.",
    });
  }

  return NextResponse.redirect(new URL("/onboarding?done=1", request.url), { status: 303 });
}

function redirectWith(request: NextRequest, params: Record<string, string>) {
  const query = new URLSearchParams(params);
  return NextResponse.redirect(new URL(`/onboarding?${query}`, request.url), { status: 303 });
}
