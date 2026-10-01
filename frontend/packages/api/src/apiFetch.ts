// The one way the panel calls `web` (PRD E8 §8). Same origin only, the session cookie
// rides along (`credentials: "same-origin"`), and a write carries the in-memory CSRF token
// or is not sent at all. Nothing here reads or writes any browser storage.
import { ApiError, CsrfMissingError, SessionChangedError } from "./errors";
import { expire, getCsrf, getEpoch } from "./session";

const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface ApiOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  json?: unknown;
  signal?: AbortSignal;
  /** A 401 is the expected "signed out" answer here; it does not end the session. */
  quiet401?: boolean;
}

/** `path` must be a same-origin path (`/api/...`); an absolute or cross-origin URL throws. */
export function sameOriginUrl(path: string): URL {
  if (!path.startsWith("/") || path.startsWith("//")) throw new Error(`apiFetch: not a same-origin path: ${path}`);
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin) throw new Error(`apiFetch: not a same-origin path: ${path}`);
  return url;
}

export async function apiFetch<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const url = sameOriginUrl(path);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (WRITES.has(method)) {
    const token = getCsrf();
    if (!token) throw new CsrfMissingError();
    headers["X-CSRF-Token"] = token;
  }
  if (opts.json !== undefined) headers["Content-Type"] = "application/json";
  const at = getEpoch();
  const res = await fetch(url, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.json === undefined ? undefined : JSON.stringify(opts.json),
    signal: opts.signal,
  });
  if (getEpoch() !== at) throw new SessionChangedError();
  const body = res.status === 204 ? undefined : await res.json().catch(() => undefined);
  if (getEpoch() !== at) throw new SessionChangedError();
  if (!res.ok) {
    if (res.status === 401 && !opts.quiet401) expire(at);
    const retry = Number(res.headers.get("Retry-After"));
    const message = typeof body?.error === "string" ? body.error : res.statusText || `HTTP ${res.status}`;
    throw new ApiError(res.status, message, Number.isFinite(retry) && retry > 0 ? retry : undefined, body);
  }
  return body as T;
}
