import { NextRequest, NextResponse } from "next/server";
import { recordEvent } from "@/lib/events";

export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  await recordEvent(request, id, "dismissed");
  return NextResponse.redirect(new URL("/matches", request.url), { status: 303 });
}
