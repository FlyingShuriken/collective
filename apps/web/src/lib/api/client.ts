import type { components } from "./generated";

type ApiErrorBody = components["schemas"]["ErrorResponse"];

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly detail: ApiErrorBody) {
    super(detail.message);
  }
}

export async function apiRequest<T>(path: `/v1/${string}`, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, {
      code: typeof body?.code === "string" ? body.code : "REQUEST_FAILED",
      message: typeof body?.message === "string" ? body.message : "The request could not be completed.",
      refresh_reference: typeof body?.refresh_reference === "string" ? body.refresh_reference : null,
    });
  }
  return response.json() as Promise<T>;
}
