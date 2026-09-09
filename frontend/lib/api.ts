/**
 * The one place the browser talks to the API (§12).
 *
 * Access tokens live in an httpOnly cookie, set by the route handlers under
 * `app/api/auth/`, and are attached here on the server. The token never reaches
 * client JavaScript, so an XSS bug cannot walk off with a session — which is
 * the whole reason for the indirection.
 */

import { cookies } from "next/headers";

export const API_BASE = process.env.CAREERPILOT_API_URL ?? "http://localhost:8000";

export const ACCESS_COOKIE = "cp_access";
export const REFRESH_COOKIE = "cp_refresh";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body?: unknown,
  ) {
    super(message);
  }
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  formData?: FormData;
  /** Skip the auth header — used by login and register. */
  anonymous?: boolean;
};

/**
 * Call the API as the signed-in user.
 *
 * RFC 7807 problem documents come back as `ApiError`, so a caller can render
 * `title` and any extension members (the parseability report, the existing
 * application behind a duplicate warning) rather than a generic failure.
 */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};

  if (!options.anonymous) {
    const store = await cookies();
    const token = store.get(ACCESS_COOKIE)?.value;
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let payload: BodyInit | undefined;
  if (options.formData) {
    payload = options.formData;
  } else if (options.body !== undefined) {
    payload = JSON.stringify(options.body);
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${API_BASE}${path}`, {
    method: options.method ?? (payload ? "POST" : "GET"),
    headers,
    body: payload,
    // Matches change when the worker runs, not when Next decides to cache.
    cache: "no-store",
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const parsed = text ? safeJson(text) : undefined;

  if (!response.ok) {
    const problem = parsed as { title?: string; detail?: string } | undefined;
    throw new ApiError(
      response.status,
      problem?.title ?? problem?.detail ?? `Request failed (${response.status})`,
      parsed,
    );
  }
  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function isSignedIn(): Promise<boolean> {
  const store = await cookies();
  return Boolean(store.get(ACCESS_COOKIE)?.value);
}

// ── Response shapes ───────────────────────────────────────────────────

export type Presentation = {
  percentile: number;
  band: string;
  summary: string;
  pool_size: number;
};

export type MatchCardData = {
  match_id: string;
  title: string;
  company: string | null;
  locations: { raw?: string; city?: string | null; country?: string | null }[];
  remote_type: string | null;
  posted_at: string | null;
  apply_url: string;
  url_status: string;
  last_verified_at: string | null;
  presentation: Presentation;
  explanation: string | null;
  gaps: string[];
  subscores: Record<string, number>;
};

export type RequirementEvidence = {
  requirement: string;
  kind: string;
  must_have: boolean;
  status: "met" | "partial" | "missing";
  similarity: number | null;
  evidence_from_cv: string | null;
};

export type MatchDetail = MatchCardData & {
  posting_id: string;
  contributions: Record<string, number>;
  requirements: RequirementEvidence[];
  excerpt: string;
  computed_at: string;
};

export type ParseabilityFinding = {
  code: string;
  severity: "blocker" | "warning" | "info";
  message: string;
  suggestion: string;
};

export type ParseabilityReport = {
  quality: number;
  is_processable: boolean;
  character_yield_per_page: number;
  sections_found: string[];
  sections_missing: string[];
  layout_damage: number;
  page_count: number;
  /** Which script the CV was read as, and whether it genuinely used both. */
  language: string;
  is_mixed_script: boolean;
  findings: ParseabilityFinding[];
};

export type ProfileSkill = {
  name: string;
  years: number | null;
  proficiency: string | null;
  source: string;
  evidence_span: [number, number];
};

export type Profile = {
  profile_id: string;
  cv_version_id: string;
  years_experience: number | null;
  seniority_level: string | null;
  locations: string[];
  work_authorization: Record<string, string>;
  languages: { lang: string; cefr: string | null }[];
  summary: string | null;
  extraction_confidence: number | null;
  skills: ProfileSkill[];
};

export type ApplicationRow = {
  id: string;
  status: string;
  applied_at: string;
  posting_id: string;
  title: string;
  company: string | null;
  apply_url: string;
  url_status: string;
  event_count: number;
};

export type WithheldRow = {
  title: string;
  company: string | null;
  reasons: { code: string; explanation: string }[];
};

export type GapRow = {
  skill: string;
  must_have: boolean;
  suggestion: string;
};

export type GapAnalysis = {
  matched: string[];
  gaps: GapRow[];
  blocking_count: number;
  note: string;
};

export type InterviewQuestion = {
  requirement: string;
  must_have: boolean;
  likely_question: string;
  your_evidence: string | null;
  status: string;
  advice: string;
};

export type InterviewPrep = {
  role: string;
  company: string | null;
  questions: InterviewQuestion[];
  note: string;
};

export type CoverLetter = {
  letter: string;
  checked: boolean;
  disclosure: string;
};
