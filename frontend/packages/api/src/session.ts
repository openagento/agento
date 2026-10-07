// The session and its teardown (PRD E2 §4.2, E8 §8). The CSRF token lives in this
// module's closure only: never in storage, a cookie the page can read, a URL or a log.
// `endSession()` is the one teardown: logout, the first 401 and a new login all call it.
import { apiFetch } from "./apiFetch";
import { ApiError } from "./errors";

export interface User { id: number; username: string; role: string; is_active: boolean }
/** The panel display settings (`admin/locale/*`); null when the `admin` module is disabled. */
export interface Display { date_format: "us" | "eu" | "iso"; timezone: string }

let csrf: string | null = null;
let user: User | null = null;
let display: Display | null = null;
let epoch = 0;
let expiredEpoch = -1;
const teardowns = new Set<() => void>();
const listeners = new Set<() => void>();
const expiredListeners = new Set<() => void>();

const notify = () => listeners.forEach((l) => l());

export const getCsrf = () => csrf;
export const getEpoch = () => epoch;
export const getUser = () => user;
export const getDisplay = () => display;

/** For `useSyncExternalStore`. */
export function subscribeUser(l: () => void): () => void {
  listeners.add(l);
  return () => { listeners.delete(l); };
}

/** Register work `endSession()` must undo (caches, streams, launch windows). */
export function onEndSession(fn: () => void): () => void {
  teardowns.add(fn);
  return () => { teardowns.delete(fn); };
}

/** Called once when a session expires (the first 401), e.g. to route to /login. */
export function onExpired(fn: () => void): () => void {
  expiredListeners.add(fn);
  return () => { expiredListeners.delete(fn); };
}

export function endSession(): void {
  epoch += 1;
  csrf = null;
  user = null;
  display = null;
  teardowns.forEach((fn) => fn());
  notify();
}

/** The first 401 of a session ends it; a second 401 from the same session does nothing. */
export function expire(atEpoch: number): void {
  if (atEpoch !== epoch || expiredEpoch === atEpoch) return;
  expiredEpoch = atEpoch;
  endSession();
  expiredListeners.forEach((fn) => fn());
}

interface SessionBody { user: User; csrf_token: string; expires_at: string; display?: Display | null }

function adopt(body: SessionBody): User {
  csrf = body.csrf_token;
  user = body.user;
  display = body.display ?? null;
  notify();
  return body.user;
}

/** `GET /api/session`: the signed-in user, or null. */
export async function boot(): Promise<User | null> {
  try {
    return adopt(await apiFetch<SessionBody>("/api/session", { quiet401: true }));
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

/** Re-read the display settings after a Config change, so a mounted page shows the new
 *  format without a reload. A failure keeps the old settings; a newer session wins. */
export async function refreshDisplay(): Promise<void> {
  const at = epoch;
  try {
    const body = await apiFetch<SessionBody>("/api/session", { quiet401: true });
    if (at !== epoch) return;
    display = body.display ?? null;
    notify();
  } catch {
    // The old settings stay; a 401 is handled by apiFetch like any other request.
  }
}

/** The one write that has no CSRF token yet. Not exported from the package: only
 *  `login()` can send it (the backend exempts only `POST /api/session` from CSRF). */
async function sendLogin(username: string, password: string): Promise<SessionBody> {
  const res = await fetch("/api/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const retry = Number(res.headers.get("Retry-After"));
    throw new ApiError(res.status, body?.error ?? res.statusText, Number.isFinite(retry) && retry > 0 ? retry : undefined);
  }
  return body as SessionBody;
}

export async function login(username: string, password: string): Promise<User> {
  endSession();
  const at = epoch;
  const body = await sendLogin(username, password);
  if (at !== epoch) throw new ApiError(409, "the session changed");
  return adopt(body);
}

export async function logout(): Promise<void> {
  try {
    await apiFetch("/api/session", { method: "DELETE", quiet401: true });
  } finally {
    endSession();
  }
}
