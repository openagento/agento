// The session and its teardown (PRD E2 §4.2, E8 §8). The CSRF token lives in this
// module's closure only: never in storage, a cookie the page can read, a URL or a log.
// `endSession()` is the one teardown: a confirmed logout, the first 401 and a new login all call it.
import { apiFetch } from "./apiFetch";
import { within } from "./deadline";
import { ApiError, SessionChangedError } from "./errors";
import { isLanguageTag, isRecord, shaped } from "./shape";

/** `acl`: the admin leaves the user holds (the admin role holds every one). */
/** `ui_locale`: the language the user stored, also when this install no longer has it. */
export interface User { id: number; username: string; role: string; is_active: boolean; acl: string[]; ui_locale: string | null }
/** The panel display settings (`admin/locale/*`); null when the `admin` module is disabled.
 *  `date_format_explicit`: an operator set the format (ENV or DB), so it wins over the language's own. */
export interface Display { date_format: "us" | "eu" | "iso"; timezone: string; date_format_explicit: boolean }
export interface Language { tag: string; name: string }
/** `effective`: the language the panel shows. `version`: the content version of its catalog. */
export interface SessionLocale { effective: string; instance: string; available: Language[]; version: string }

let csrf: string | null = null;
let user: User | null = null;
let display: Display | null = null;
let locale: SessionLocale | null = null;
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
export const getLocale = () => locale;

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
  locale = null;
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

interface SessionBody { user: User; csrf_token: string; expires_at: string; display?: Display | null; locale?: SessionLocale | null }
interface SavedLocale { ui_locale: string; locale: SessionLocale }

const isLanguage = (value: unknown): value is Language =>
  isRecord(value) && isLanguageTag(value.tag) && typeof value.name === "string";

const isSessionLocale = (value: unknown): value is SessionLocale =>
  isRecord(value) && isLanguageTag(value.effective) && isLanguageTag(value.instance)
  && typeof value.version === "string" && Array.isArray(value.available) && value.available.every(isLanguage);

/** The language data of a session answer: the stored language and the `locale` block. */
const isSessionBody = (body: unknown): body is SessionBody =>
  isRecord(body) && isRecord(body.user) && (body.user.ui_locale === null || isLanguageTag(body.user.ui_locale))
  && (body.locale === undefined || body.locale === null || isSessionLocale(body.locale));

const isSavedLocale = (body: unknown): body is SavedLocale =>
  isRecord(body) && isLanguageTag(body.ui_locale) && isSessionLocale(body.locale);

const readSession = async () => shaped(await apiFetch("/api/session", { quiet401: true }), isSessionBody);

function adopt(body: SessionBody): User {
  csrf = body.csrf_token;
  user = body.user;
  display = body.display ?? null;
  locale = body.locale ?? null;
  notify();
  return body.user;
}

/** `GET /api/session`: the signed-in user, or null. */
export async function boot(): Promise<User | null> {
  const at = epoch;
  try {
    const body = await readSession();
    if (at !== epoch) throw new SessionChangedError();
    return adopt(body);
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
    const body = await readSession();
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
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const retry = Number(res.headers.get("Retry-After"));
    throw new ApiError(res.status, isRecord(body) && typeof body.error === "string" ? body.error : res.statusText, Number.isFinite(retry) && retry > 0 ? retry : undefined, body ?? undefined);
  }
  return shaped(body, isSessionBody);
}

/** The longest wait for the answer of the logout request. The next sign-in waits for it, and the
 *  user waits too, so it is a few seconds: longer than a slow answer of a healthy server, shorter
 *  than the time after which a user reloads the page. At the deadline the request is cancelled. */
export const LOGOUT_DEADLINE_MS = 5000;

/** Settles when no logout request of this tab waits for its answer. That answer clears the
 *  session cookie, and the browser applies cookies in the order of arrival: a sign-in that is
 *  answered first would lose its cookie to a late logout answer. So `login()` sends nothing
 *  before this settles. */
let pendingLogout: Promise<void> = Promise.resolve();

export async function login(username: string, password: string): Promise<User> {
  endSession();
  const at = epoch;
  await pendingLogout;
  if (at !== epoch) throw new ApiError(409, "the session changed");
  const body = await sendLogin(username, password);
  if (at !== epoch) throw new ApiError(409, "the session changed");
  return adopt(body);
}

/** Ends the session it was called in, when the server confirmed the end (2xx) or says that the
 *  session is already gone (401). Any other outcome (no answer at the deadline, no network, a
 *  server error) rejects and the session stays: the server can still hold it. When that session
 *  ended during the request, the session that runs now is another one and stays. */
export async function logout(): Promise<void> {
  const at = epoch;
  const request = within(LOGOUT_DEADLINE_MS, (signal) => apiFetch("/api/session", { method: "DELETE", quiet401: true, signal }));
  const earlier = pendingLogout;
  pendingLogout = request.then(() => earlier, () => earlier);
  try {
    await request;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) throw e;
  }
  if (at === epoch) endSession();
}

/** `PATCH /api/profile/locale`: stores the language of the signed-in account. The account is the
 *  session's; the body names no user. `version` is the catalog version the caller read: it goes
 *  in `If-Match`, and the server refuses (`CATALOG_CHANGED`) when its catalog is another one. */
export async function saveLocale(tag: string, version: string): Promise<SessionLocale> {
  const at = epoch;
  const saved = shaped(await apiFetch("/api/profile/locale", {
    method: "PATCH", json: { ui_locale: tag }, ifMatch: version,
  }), isSavedLocale);
  if (at !== epoch) throw new SessionChangedError();
  if (user) user = { ...user, ui_locale: saved.ui_locale };
  locale = saved.locale;
  notify();
  return saved.locale;
}
