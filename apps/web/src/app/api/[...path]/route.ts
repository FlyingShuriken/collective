import { NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

function failure(status: number, code: string, message: string) {
  return Response.json(
    { code, message, refresh_reference: null },
    { status, headers: { "Cache-Control": "private, no-store" } },
  );
}

async function forward(request: NextRequest, context: RouteContext<"/api/[...path]">) {
  const { path } = await context.params;
  if (
    path[0] !== "v1" || !["me", "hub", "businesses"].includes(path[1]) ||
    path.some((segment) => !/^[a-zA-Z0-9_-]+$/.test(segment))
  ) {
    return failure(404, "NOT_FOUND", "Resource not found.");
  }
  // Next's internal URL can use localhost while the browser uses the request Host.
  const origin = `${request.nextUrl.protocol}//${request.headers.get("host")}`;
  if (
    !["GET", "HEAD"].includes(request.method) &&
    request.headers.get("origin") !== origin
  ) {
    return failure(403, "FORBIDDEN", "This action is not permitted.");
  }
  const base = process.env.COLLECTIVE_API_URL;
  if (!base) return failure(503, "NOT_READY", "The application is not ready.");
  try {
    const target = new URL(base);
    if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) {
      return failure(503, "NOT_READY", "The application is not ready.");
    }
    target.pathname = "/" + path.join("/");
    target.search = request.nextUrl.search;
    const supabase = await createClient();
    const { data: { user }, error } = await supabase.auth.getUser();
    if (error || !user) return failure(401, "UNAUTHENTICATED", "A valid sign-in is required.");
    const { data: { session } } = await supabase.auth.getSession();
    if (!session) return failure(401, "UNAUTHENTICATED", "A valid sign-in is required.");
    const headers = new Headers({ Authorization: "Bearer " + session.access_token });
    for (const name of ["content-type", "idempotency-key", "accept"]) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    const options: RequestInit & { duplex?: "half" } = {
      method: request.method,
      headers,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(path.includes("sources") ? 120_000 : 35_000),
    };
    if (!["GET", "HEAD"].includes(request.method)) {
      options.body = request.body;
      options.duplex = "half";
    }
    const upstream = await fetch(target, options);
    if (upstream.status >= 300 && upstream.status < 400) {
      return failure(502, "UPSTREAM_UNAVAILABLE", "The application is temporarily unavailable.");
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        ...(upstream.headers.has("content-disposition") ? { "Content-Disposition": upstream.headers.get("content-disposition")! } : {}),
      },
    });
  } catch {
    return failure(503, "UPSTREAM_UNAVAILABLE", "The application is temporarily unavailable.");
  }
}

export {
  forward as GET, forward as HEAD, forward as POST,
  forward as PUT, forward as PATCH, forward as DELETE,
};
